# RTTR Architecture

**RTTR** is the core testing infrastructure layer providing:

- **Video Recording**: Automatic desktop recording (ffmpeg, 2 FPS H.264) of all pytest sessions
- **Frame Extraction**: Automatic PNG frame extraction for failed tests with frame-level video navigation
- **Base Classes**: `BaseModel` widget infrastructure and `ControlType` enum
- **UINode & Serialization**: JSON serialization for UI automation objects

See [rttr/README.md](../README.md) for detailed documentation on video recording and frame analysis.

## Quick Navigation

- **Video Recording & Frame Analysis**: See [rttr/README.md](../README.md)
- **BaseModel Class**: See `rttr/base.py`
- **Type Definitions**: `rttr/__init__.py` for `UINode`, `ControlType`
