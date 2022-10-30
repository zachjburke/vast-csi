from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Optional, final, TypeVar

import grpc

from .logging import logger
from . import csi_types as types
from plumbum import local

from .exceptions import Abort
from .configuration import StorageClassOptions

CreatedVolumeT = TypeVar("CreatedVolumeT")


class VolumeBuilderI(ABC):
    """Base Volume Builder interface"""

    @abstractmethod
    def build_volume_name(self, **kwargs) -> str:
        """
        Final implementation should build volume name from provided argumenents and/or from other params
        depends on conditions.
        """
        ...

    def get_requested_capacity(self) -> int:
        """Final implementation should return requested capacity based on provided params and/or other inputs"""
        ...

    def get_existing_capacity(self) -> int:
        """Final implementation should return existing voume capacity based on provided params and/or other inputs"""
        ...

    def build_volume(self, **kwargs) -> CreatedVolumeT:
        """
        Final implementation shoud perform final actions for creationg volume and/or return all necessary
        data to create volume.
        """
        ...


# ----------------------------------------------------------------------------------------------------------------------
# Final builders
# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class BaseBuilder(VolumeBuilderI):
    """Common builder with shared methods/attributes"""

    controller: "ControllerServicer"
    configuration: "CONF"

    name: str  # Name of volume or snapshot
    capacity_range: Optional[int]  # Optional desired volume capacity
    storage_options: StorageClassOptions
    pvc_name: Optional[str]
    pvc_namespace: Optional[str]
    volume_content_source: Optional[str]  # Either volume or snapshot
    ephemeral_volume_name: Optional[str] = None

    def get_requested_capacity(self) -> int:
        """Return desired allocated capacity if provided else return 0"""
        return self.capacity_range.required_bytes if self.capacity_range else 0

    def get_existing_capacity(self) -> Optional[int]:
        """Return capacity of requested quota"""
        quota = self.controller.vms_session.get_quota(self.name)
        if quota:
            return quota.hard_limit


@final
class VolumeBuilder(BaseBuilder):
    """Builder for k8s PersistentVolumeClaim, PersistentVolume etc."""

    def build_volume_name(self, volume_name_fmt: str) -> str:
        """Build volume name using format csi:{namespace}:{name}:{id}"""
        volume_id = self.name
        if self.ephemeral_volume_name:
            volume_name = self.ephemeral_volume_name
        elif self.pvc_name and self.pvc_namespace:
                volume_name = volume_name_fmt.format(
                    namespace=self.pvc_namespace, name=self.pvc_name, id=volume_id
                )
        else:
            volume_name = f"csi-{volume_id}"

        if len(volume_name) > 64:
            logger.warning(
                f"cropping volume name ({len(volume_name)}>64): {volume_name}"
            )
            volume_name = volume_name[:64]  # crop to Vast's max-length
        return volume_name

    def build_volume(self) -> types.Volume:
        """
        Main build entrypoint for volumes.
        Create volume from pvc, pv etc.
        """
        volume_context = {}
        root_export = local.path(self.storage_options.root_export_path)

        volume_name = self.build_volume_name(
            volume_name_fmt=self.storage_options.volume_name_fmt
        )
        requested_capacity = self.get_requested_capacity()

        # Check if volume with provided name but another capacity already exists.
        if existing_capacity := self.get_existing_capacity():
            if existing_capacity != requested_capacity:
                raise Abort(
                    grpc.StatusCode.ALREADY_EXISTS,
                    "Volume already exists with different capacity than requested"
                    f"({existing_capacity})",
                )

        data = dict(
            create_dir=True,
            name=volume_name,
            path=str(root_export[self.name]),
        )
        if requested_capacity:
            data.update(hard_limit=requested_capacity)
        quota = self.controller.vms_session.create_quota(data=data)
        volume_context.update(quota_id=str(quota.id))

        return types.Volume(
            capacity_bytes=requested_capacity,
            volume_id=self.name,
            volume_context={**volume_context, **self.storage_options.dict()},
        )


@final
class SnapshotBuilder(BaseBuilder):
    """Builder for k8s Snapshots."""

    def build_volume_name(self) -> str:
        snapshot_id = self.volume_content_source.snapshot.snapshot_id
        snapshot = self.controller.vms_session.get_snapshot(snapshot_id=snapshot_id)
        snapshot_path = local.path(snapshot.path)

        # Compute root_export from snapthot path. This value should be passed as context for appropriate
        # mounting within 'ControllerPublishVolume' endpoint
        self.root_export = snapshot_path.parent

        path = snapshot_path / ".snapshot" / snapshot.name
        return str(path.relative_to(self.root_export))

    def build_volume(self) -> types.Volume:
        """
        Main entry point for snapshots.
        Create snapshot representation.
        """
        snapshot_base_path = self.build_volume_name()
        snapshot_id = self.volume_content_source.snapshot.snapshot_id
        # Requested capacity is always 0 for snapshots.
        return types.Volume(
            capacity_bytes=0,
            volume_id=self.name,
            content_source=types.VolumeContentSource(
                snapshot=types.SnapshotSource(snapshot_id=snapshot_id)
            ),
            volume_context={
                # Pass vms options via context
                **self.storage_options.dict(),
                "snapshot_base_path": snapshot_base_path,
                "root_export": self.root_export,
            },
        )


@final
class TestVolumeBuilder(BaseBuilder):
    """Test volumes builder for sanity checks"""

    def build_volume_name(self) -> str:
        pass

    def get_existing_capacity(self) -> Optional[int]:
        volume = self.controller._to_mock_volume(self.name)
        if volume:
            return volume.capacity_bytes

    def build_volume(self) -> types.Volume:
        """Main build entrypoint for tests"""
        requested_capacity = self.get_requested_capacity()

        if existing_capacity := self.get_existing_capacity():
            if existing_capacity != requested_capacity:
                raise Abort(
                    grpc.StatusCode.ALREADY_EXISTS,
                    "Volume already exists with different capacity than requested"
                    f"({existing_capacity})",
                )

        vol_dir = self.controller._mock_mount[self.name]
        vol_dir.mkdir()

        volume = types.Volume(
            capacity_bytes=requested_capacity,
            volume_id=self.name,
        )

        with self.controller.mock_vol_db[self.name].open("wb") as f:
            f.write(volume.SerializeToString())
        return volume
