# three.js (vendored)

Used only by `../../hero.js`, the 3D logo on the documentation's front page. It is vendored so the
site makes no third-party requests.

| | |
|---|---|
| Version | 0.186.1 |
| Source | npm package `three`, the files below, unmodified (`build/three.module.js` and `build/three.core.js` sit at the top here) |
| Licence | MIT, see `LICENSE`; the Draco decoder in `examples/jsm/libs/draco` is Apache-2.0, see its `LICENSE` |
| `three.module.js` | sha256-kFIELWdssP3B3f7+GTBT80t6wFE6YW/axFNdSZh4Euo= |
| `three.core.js` | sha256-nt3gArBmqaBWdqYSf2dzW2K685m96lKfL34xZX2naeY= |
| `examples/jsm/loaders/GLTFLoader.js` | sha256-ExwPeMAdGTaK5JXKpls62qEEh4EKNqBbtZAbdpo1rBY= |
| `examples/jsm/loaders/DRACOLoader.js` | sha256-/a/fSqEbvGXYmtszjv2ES+35QHNwL5BJJA/x8epIrV8= |
| `examples/jsm/utils/BufferGeometryUtils.js` | sha256-n7Y0J85mQfoU/Quv+cxNG1+cPYX9CEvy6Q6APETsF5c= |
| `examples/jsm/utils/SkeletonUtils.js` | sha256-sWMqcDIGw9gw3p/L5RVpZ3DQS3GhXua1CvptLDKYyG8= |
| `examples/jsm/libs/draco/gltf/draco_decoder.wasm` | sha256-poDZJ77Zy4ZN29Y1IYaIka8r++dVCSdhtIN0h2GN+Kw= |
| `examples/jsm/libs/draco/gltf/draco_wasm_wrapper.js` | sha256-i7KVLSun1n4UFPjfgZQQywQ0pma+U/Zx//dfaIQ9dvY= |
| `LICENSE` | sha256-izeOvmDi/lABWMsKxxy16LfZKVPCq8xjoOuQSZZTtbw= |
| `examples/jsm/libs/draco/LICENSE` | from `google/draco` 1.5.7, sha256-03CbD7S4qUu7HQK4ouSE8liw2cXFoB+UA5Hz/mYs0aQ= |

To update: download the same files for the new version, check each file's SHA-256 against the
hashes jsDelivr publishes for the npm package
(`https://data.jsdelivr.com/v1/packages/npm/three@<version>?structure=flat`), replace them here
and update this table. Dependabot does not watch these files.

The model, `../../brand/wardowl.glb`, uses Draco geometry and WebP textures
(`KHR_draco_mesh_compression`, `EXT_texture_webp`), which is why the Draco decoder is here.
