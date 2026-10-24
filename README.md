# Killzone Asset Tools for Blender

Killzone Asset Tools is a Blender extension and companion toolkit for
inspecting and importing Killzone assets. It targets **Blender 5.2.x** and
uses strict, bounded parsers so that measured behavior stays separate from
unverified assumptions.

Native game formats are currently **read-only**. The project can import proven
data paths and preserve source metadata, but it does not encode edited Blender
content back into native Killzone resources.

For format layouts, corpus counts, decoding equations, runtime evidence, and
known unknowns, see the
[technical reference](./docs/technical-reference.md).

## Highlights

- **Faster imports (0.14.0)** — only textures referenced by the selected
  materials are decoded, once per token; mip validation no longer unswizzles
  discarded mips; the complementary-alpha bank is built only when a layer
  can sample it; swizzle and palette decoding use cached lookup tables;
  meshes are written with bulk `foreach_set`; and images pack pre-encoded
  lossless PNGs. Re-importing an asset reuses its packed images.
- **More faithful material previews (0.14.0)** — V4-8 and LAYERBUMPDOT3
  products are evaluated in display-gamma space with GS saturation, mixed
  repeat/clamp wrapping no longer draws tile seams, alpha-tested cutouts use
  dithered transparency, and the scene can switch to the Standard view
  transform so AgX does not tone-map unlit colors. Optional artist modes add
  LAYERBUMPDOT3 lightmap layers over WRITE bases and a texture fallback for
  unresolved materials.
- **UV and texturing improvements** — import static and skinned UVs in
  Blender's V-up convention, preserve authored repeat-tile offsets and dual
  UV layers, and apply verified axis-aligned material scale/offset transforms
  used by texture atlases.
- **Map prop instancing** — reconstruct direct world geometry plus authored
  `StaticMeshInstance` and `CollisionMeshInstance` visual occurrences. Every
  placement becomes a distinct Blender object with its serialized transform,
  while repeated props safely share immutable mesh datablocks.
- **Additional texture and material formats** — decode the proven
  `INDEX_4`, `INDEX_8`, `ALPHA_4`, `ALPHA_8`, `RGBA_8888`, and `RGB_888`
  texture encodings and build bounded previews for `WRITE`, vertex-modulated
  `ALPHAADD`, `ALPHAADD + LAYERBUMPDOT3`, and
  `MASKEDWRITE + ALPHAADD` material families.
- Cache importable native assets from an authorized ISO or DAT directory.
- Import naturally ordered maps with optional packed textures.
- Import standalone static props and skinned models with Skeletons.
- Bake compatible model-local or explicitly selected shared animations.
- Preserve unchanged interchange meshes and attached original blobs
  byte-for-byte.
- Inspect authorized Killzone disc images and native container catalogs
  without modifying them.
- Import standalone skinned characters with source-order Skeletons, rigid
  vertex assignments, materials, and decoded textures.
- Decode variant-5 animation banks and bake clips as persistent Blender 5.2
  slotted Actions.
- Represent unresolved materials, palettes, transforms, and runtime-only
  systems explicitly instead of inventing conversions.
- Validate parsers with pure-Python tests and an actual headless Blender smoke
  test.

## Screenshots

### KZ Tools workflow

The sidebar provides cached selectors for maps and static or skinned models.

<p align="center">
  <img
    src="docs/images/Addon_UI.png"
    alt="Killzone Asset Tools sidebar with map and model import controls"
    width="377"
  >
</p>

### Imported map

`1. Helghast Assault - HQ Grounds` imported with packed native textures and
bounded visible material previews:

![Imported Helghast Assault HQ Grounds map](docs/images/HelghastAssaultHQ_ImportedMap.png)

### Models and animations

| Imported hovertank | Player 3rd Luger animation |
|---|---|
| <img src="docs/images/Hovertank_Import.png" alt="Imported Killzone hovertank model"> | <img src="docs/images/Luger_Animations.png" alt="Player 3rd Luger model using an imported animation Action"> |

## Support at a glance

| Area | Status |
|---|---|
| Killzone Asset Bundle import/export | Implemented; UI temporarily hidden |
| Static meshes, normals, UVs, colors, and material slots | Supported, including dual UVs, repeat offsets, and host-corrected V orientation |
| ISO/container discovery and catalog reporting | Read-only |
| Native background and embedded textures | Proven indexed, alpha, RGB, and RGBA encodings; read-only |
| Native static level geometry | Experimental default-LOD graph with direct and instanced occurrences |
| Native skinned characters and Skeletons | Experimental standalone import |
| Native animation Actions | Experimental variant-5 import |
| Native materials | Structural assignment plus bounded single- and two-layer previews |
| Runtime decals | Policies preserved; projected geometry not reconstructed |
| Collision-backed visual props | Authored placements imported with shared mesh data; collision geometry is metadata only |
| Gameplay entities, AI, and effects | Not imported |
| Edited native resource export | Not supported |

The project-owned `.kzm` interchange format is not a native Killzone format.
Its coordinate convention is right-handed, Z-up, -Y-forward, in meters.

## Requirements

- Blender **5.2.x**
- Python for the standalone inspection tools
- An authorized Killzone disc image or extracted native containers for native
  asset workflows

No game data is included in this repository. Disc images, DAT containers,
generated packages, and probe output are excluded by
[`.gitignore`](./.gitignore).

## Install in Blender

The current add-on version is **0.14.0**.

Build the extension package from the repository root:

```powershell
New-Item -ItemType Directory -Force '.\dist' | Out-Null
& 'D:\Blender Foundation\Blender 5.2\blender.exe' `
  --command extension build `
  --source-dir '.\io_scene_killzone' `
  --output-dir '.\dist'
```

In Blender, open **Edit > Preferences > Extensions**, choose
**Install from Disk**, and select the generated ZIP. Operators are available
in the 3D View sidebar under **KZ Tools**.

## Usage

See the [usage guide](./docs/usage.md) for asset setup, map and model imports,
material preview options, animation selection, troubleshooting, and companion
tools.

## Important boundaries

- Native parsers and importers are read-only.
- Native material graphs are inspectable diagnostics, not a claim of exact
  final game shading.
- Generated coordinate routes, some transforms, dynamic palettes, and other
  runtime state remain explicitly unresolved.
- Static level imports place the visual mesh references carried by
  `CollisionMeshInstance`, but do not decode collision geometry. Skinned
  actors, paths, AI, entities, effects, lights, gameplay behavior, and
  higher-LOD selection are not imported.
- Animation import preserves clip identity, binding diagnostics, timing, and
  decomposition residuals. Blender channel approximations are rejected unless
  the caller explicitly permits a measured tolerance.
- Native export remains blocked until unchanged serialization, edited-data
  encoding, archive repacking, and game-side acceptance are independently
  demonstrated.

## Repository layout

```text
io_scene_killzone/       Blender extension and core parsers
tools/                   Standalone inspection and import utilities
tests/                   Pure-Python and Blender smoke tests
docs/technical-reference.md
                         Format evidence and implementation contracts
docs/usage.md            Blender workflows and companion tools
docs/architecture.md     Implemented architecture and evidence-gated roadmap
```

## Validate

Run the pure-Python format and safety tests:

```powershell
python -m unittest discover -s tests -p 'test_*.py'
```

Run the Blender import/edit/export smoke test:

```powershell
& 'D:\Blender Foundation\Blender 5.2\blender.exe' `
  --background --factory-startup `
  --python '.\tests\blender_smoke.py'
```

The smoke fixture is generated procedurally and contains no game data.
The same smoke test runs without a Blender install through the `bpy` wheel:

```bash
python -m pip install bpy==5.2.0
python tests/blender_smoke.py
```

GitHub Actions runs lint, the unit tests, and this smoke test on every push
and pull request (`.github/workflows/tests.yml`).

## Documentation

- [Usage guide](./docs/usage.md) - asset setup, Blender import workflows,
  material preview options, troubleshooting, and companion tools
- [Technical reference](./docs/technical-reference.md) - native formats,
  evidence, equations, diagnostics, and unresolved boundaries
- [Architecture](./docs/architecture.md) - implemented module boundaries,
  data flows, decisions, and evidence-gated next steps
- [Map prop instancing research](./docs/map_prop_instancing_research.md) -
  occurrence identity, transform handling, mesh sharing, and retail fixtures
- [Material and shader research](./docs/material_shader_research.md) -
  texture formats, UV routing, GS state, bounded previews, and open questions

## License

The extension is distributed under `GPL-3.0-or-later`.
