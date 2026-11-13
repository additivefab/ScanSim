"""Producer-side projection of declared settings; never expose model state to reconstruction."""

from copy import deepcopy
from dataclasses import replace

from zlscan.contracts.calibration import Binding, CaptureDescription, EpochBinding
from zlscan.engine.physical_run import simulation_binding
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import scan_blocks


def describe_recording(recorder) -> CaptureDescription | None:
    """Called under the recorder lock, after validating its complete checkpoint boundary.

    Reset changes the logical epoch only: effective physical settings persist. Future declared
    epochs and epochs that delivered no blocks are omitted. Payload arrays are not retained.
    """
    raw_path = recorder.directory / "raw.zlraw"
    raw = scan_blocks(raw_path, lambda *_: None)
    if raw.errors or raw.session is None:
        raise ValueError("cannot describe damaged recording bytes")
    if not raw.blocks:
        return None
    observed = {block.epoch for block in raw.blocks}
    configs = deepcopy(recorder.base)
    entries = []
    for epoch in recorder.controller.epochs:
        # A reset has an empty config, so it deliberately leaves the physical settings intact.
        for key, value in epoch.config.items():
            _, channel, section, field = key.split(".")
            cfg = configs[int(channel)]
            setattr(cfg, section, replace(getattr(cfg, section), **{field: value}))
        if epoch.id not in observed:
            continue
        identities = {
            str(channel): simulation_binding(cfg).simulation_hash
            for channel, cfg in configs.items()
        }
        # Keep the established single-channel identity interoperable with static scans.
        identity = (
            identities["0"]
            if set(identities) == {"0"}
            else recorder._hash(
                {"format": "zlscan-multichannel-settings/1", "channels": identities}
            )
        )
        entries.append(
            EpochBinding(
                epoch=epoch.id,
                binding=Binding(
                    simulation_hash=identity,
                    channel_roles=raw.session.channel_roles,
                    epoch_range=(epoch.id, epoch.id),
                ),
            )
        )
    if {entry.epoch for entry in entries} != observed:
        raise ValueError("recording has an epoch absent from its control history")
    return CaptureDescription(
        version=2,
        raw_sha256=sha256_file(raw_path),
        binding=Binding(
            channel_roles=raw.session.channel_roles, epoch_range=(min(observed), max(observed))
        ),
        lines=max(block.line_number for block in raw.blocks) + 1,
        epoch_bindings=entries,
    )
