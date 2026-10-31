"""What the snapshot service needs from the sandbox layer.

Phase 3 implements this against Cube. The service never starts or creates a VM; it
only pauses, resumes, stops an instance, and reads the profile's node directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class NodeHost(Protocol):
    def state_dir(self, profile_id: str) -> Path:
        """Node directory mounted as the profile's Hermes home. Stable across instances."""
        ...

    def pause(self, instance_id: str) -> None: ...

    def resume(self, instance_id: str) -> None: ...

    def stop(self, instance_id: str) -> None: ...
