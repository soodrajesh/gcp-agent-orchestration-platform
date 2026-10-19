"""Cloud Run admin surface the platform needs: read a service's revisions/traffic, and shift
traffic. Behind a Protocol so the logic that uses it is tested with a fake, and so the real
client's blocking calls stay in one place."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RevisionInfo:
    name: str  # short name, e.g. ao-demo-target-00002-abc
    created: datetime
    ready: bool


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    revisions: tuple[RevisionInfo, ...]  # newest first
    traffic: dict[str, int] = field(default_factory=dict)  # revision -> percent (resolved)

    @property
    def serving(self) -> str | None:
        """The revision receiving the most traffic."""
        return max(self.traffic, key=self.traffic.get) if self.traffic else None


class RunAdmin(Protocol):
    async def get(self, service: str) -> ServiceInfo: ...
    async def set_traffic(self, service: str, revision: str) -> None:
        """Send 100% of traffic to `revision`."""
        ...


class GcpRunAdmin:
    def __init__(self, project: str, region: str) -> None:
        from google.cloud import run_v2

        self._run, self._project, self._region = run_v2, project, region
        self._services = run_v2.ServicesClient()
        self._revisions = run_v2.RevisionsClient()

    def _path(self, service: str) -> str:
        return f"projects/{self._project}/locations/{self._region}/services/{service}"

    def _get_sync(self, service: str) -> ServiceInfo:
        svc = self._services.get_service(name=self._path(service))
        revs = []
        for r in self._revisions.list_revisions(parent=self._path(service)):
            ready = any(
                c.type_ == "Ready" and c.state.name == "CONDITION_SUCCEEDED" for c in r.conditions
            )
            revs.append(RevisionInfo(r.name.rsplit("/", 1)[-1], r.create_time, ready))
        revs.sort(key=lambda x: x.created, reverse=True)
        traffic = {t.revision: t.percent for t in svc.traffic_statuses if t.revision and t.percent}
        return ServiceInfo(service, tuple(revs), traffic)

    def _set_sync(self, service: str, revision: str) -> None:
        from google.protobuf import field_mask_pb2

        run = self._run
        svc = self._services.get_service(name=self._path(service))
        svc.traffic = [
            run.TrafficTarget(
                type_=run.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                revision=revision,
                percent=100,
            )
        ]
        # Deliberately NOT op.result(): operations are project-level resources, so a service-scoped
        # run.developer binding cannot poll them. That raised after the update had already been
        # applied, and the retry then saw "state changed" (found live, ADR 0003). Poll the service
        # itself instead, which this identity is allowed to read.
        self._services.update_service(
            request=run.UpdateServiceRequest(
                service=svc, update_mask=field_mask_pb2.FieldMask(paths=["traffic"])
            )
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._get_sync(service).traffic.get(revision) == 100:
                return
            time.sleep(2)
        raise TimeoutError(f"traffic for {service} did not reach {revision} within 90s")

    async def get(self, service: str) -> ServiceInfo:
        return await asyncio.to_thread(self._get_sync, service)

    async def set_traffic(self, service: str, revision: str) -> None:
        await asyncio.to_thread(self._set_sync, service, revision)


class FakeRunAdmin:
    """In-memory Cloud Run for tests. Records every mutation."""

    def __init__(self, services: dict[str, ServiceInfo]) -> None:
        self.services, self.mutations = dict(services), []

    async def get(self, service: str) -> ServiceInfo:
        if service not in self.services:
            raise KeyError(service)
        return self.services[service]

    async def set_traffic(self, service: str, revision: str) -> None:
        self.mutations.append((service, revision))
        old = self.services[service]
        self.services[service] = ServiceInfo(service, old.revisions, {revision: 100})
