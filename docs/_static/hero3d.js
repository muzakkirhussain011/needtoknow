// The 3D logo on the documentation's front page: a document whose readable lines are printed in
// teal and one line is redacted by a black block standing proud of the page. The only animation
// carries the idea: the redaction block eases out of the page and back, while the model turns
// gently toward the pointer.
//
// Progressive enhancement: the page first shows a still render of the same scene. The live model
// replaces it only when WebGL is available, the reader has not asked for reduced motion, and the
// hero is on screen; it pauses when scrolled away or when the tab is hidden. three.js is vendored
// next to this file (vendor/three, see its README), so the page makes no third-party requests.

export const ASPECT = 1 / 0.82; // width / height of the hero stage and of the still renders
export const REST = { yaw: -0.5, pitch: 0.16 }; // the pose of the still renders and of the first frame
export const OPENING = 2250; // ms into the animation where stills are taken: the block fully out

export function buildLogo(THREE, renderer) {
  const scene = new THREE.Scene();

  // A neutral studio, rendered once into an environment map, for soft satin reflections.
  const studio = new THREE.Scene();
  studio.add(new THREE.Mesh(new THREE.BoxGeometry(30, 30, 30), new THREE.MeshBasicMaterial({ color: 0x3c4250, side: THREE.BackSide })));
  const softbox = (w, h, strength, position) => {
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(w, h),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(0xffffff).multiplyScalar(strength), side: THREE.DoubleSide }),
    );
    mesh.position.set(...position);
    mesh.lookAt(0, 0, 0);
    studio.add(mesh);
  };
  softbox(10, 6, 3.5, [-7, 7, 8]);
  softbox(5, 10, 1.6, [9, 1, 5]);
  softbox(14, 3, 0.8, [0, -8, 5]);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(studio, 0.04).texture;
  pmrem.dispose();

  const camera = new THREE.PerspectiveCamera(30, ASPECT, 0.1, 100);
  camera.position.set(0, 0.6, 12.5);
  camera.lookAt(0, -0.2, 0);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x50586a, 1.0));
  const key = new THREE.DirectionalLight(0xffffff, 4);
  key.position.set(-4, 7, 8);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, { left: -4, right: 4, top: 4, bottom: -4, near: 1, far: 30 });
  key.shadow.bias = -0.0003;
  key.shadow.radius = 4;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 1.2);
  fill.position.set(6, 1, 6);
  scene.add(fill);
  const rim = new THREE.PointLight(0x3dd6c0, 50); // a faint brand-coloured edge light
  rim.position.set(4.5, 2, -4);
  scene.add(rim);

  const rounded = (w, h, r) => {
    const x = -w / 2, y = -h / 2, s = new THREE.Shape();
    s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r);
    s.lineTo(x + w, y + h - r); s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    s.lineTo(x + r, y + h); s.quadraticCurveTo(x, y + h, x, y + h - r);
    s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
    return s;
  };
  const slab = (w, h, r, depth, bevel, material) => {
    const geometry = new THREE.ExtrudeGeometry(rounded(w, h, r), {
      depth, bevelEnabled: true, bevelThickness: bevel, bevelSize: bevel, bevelSegments: 6, curveSegments: 16,
    });
    geometry.translate(0, 0, -depth / 2);
    geometry.computeVertexNormals();
    const mesh = new THREE.Mesh(geometry, material);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
  };
  const satin = (color, roughness, metalness = 0, clearcoat = 0) =>
    new THREE.MeshPhysicalMaterial({ color, roughness, metalness, clearcoat, clearcoatRoughness: 0.4 });
  const navy = satin(0x1b2238, 0.42, 0.2, 0.35);
  const paper = satin(0xf6f7f9, 0.62);
  const teal = satin(0x0e9f8e, 0.48);
  const ink = satin(0x0b0e16, 0.36, 0.25, 0.5);

  const logo = new THREE.Group();
  const frame = slab(3.2, 4.0, 0.22, 0.28, 0.05, navy);
  frame.position.z = -0.16;
  logo.add(frame);
  const page = slab(2.86, 3.66, 0.14, 0.08, 0.025, paper);
  page.position.z = 0.02;
  logo.add(page);
  const pageFront = 0.02 + 0.04 + 0.025;
  // Printed lines: thin teal bars, almost flush with the page.
  for (const [width, y] of [[2.1, 1.05], [2.1, -0.35], [1.3, -1.05]]) {
    const line = slab(width, 0.3, 0.05, 0.02, 0.012, teal);
    line.position.set(-1.05 + width / 2, y, pageFront + 0.022);
    logo.add(line);
  }
  // The redaction: a black block standing proud of the page.
  const block = slab(2.1, 0.46, 0.06, 0.2, 0.03, ink);
  const blockRest = pageFront + 0.13;
  block.position.set(0, 0.35, blockRest);
  logo.add(block);
  scene.add(logo);

  // A soft contact shadow under the model.
  const shade = document.createElement("canvas");
  shade.width = shade.height = 128;
  const g = shade.getContext("2d");
  const gradient = g.createRadialGradient(64, 64, 2, 64, 64, 64);
  gradient.addColorStop(0, "rgba(12,16,28,0.38)");
  gradient.addColorStop(1, "rgba(12,16,28,0)");
  g.fillStyle = gradient;
  g.fillRect(0, 0, 128, 128);
  const contact = new THREE.Mesh(
    new THREE.PlaneGeometry(4.4, 1.5),
    new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(shade), transparent: true, depthWrite: false }),
  );
  contact.rotation.x = -Math.PI / 2;
  contact.position.y = -2.55;
  scene.add(contact);

  // Pose the scene at time `t` (milliseconds) with the given rotation.
  function update(t, yaw, pitch) {
    logo.rotation.set(pitch, yaw, 0);
    logo.position.y = Math.sin(t / 3200) * 0.05;
    // Every 9 seconds the block eases out of the page and back, resting flush in between.
    const phase = (t % 9000) / 9000;
    const out = phase < 0.5 ? 0.5 - 0.5 * Math.cos(phase * 2 * Math.PI * 2) : 0;
    block.position.z = blockRest + out * 0.38;
  }

  return { scene, camera, update };
}

export function createRenderer(THREE, options = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power", ...options });
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.toneMapping = THREE.NeutralToneMapping; // keeps the brand colours close to their hex values
  return renderer;
}

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

function start(THREE, stage) {
  const still = stage.querySelector("img");
  const renderer = createRenderer(THREE);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const canvas = renderer.domElement;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", still ? still.alt : "needtoknow logo");
  stage.appendChild(canvas);
  const { scene, camera, update } = buildLogo(THREE, renderer);

  const resize = () => {
    const width = stage.clientWidth, height = stage.clientHeight;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  };
  resize();
  new ResizeObserver(resize).observe(stage);

  // The model turns toward the pointer; dragging turns it further; when idle it sways slowly.
  let yaw = REST.yaw, pitch = REST.pitch, targetYaw = yaw, targetPitch = pitch;
  let dragging = false, hovering = false, nx = 0, ny = 0, lastX = 0, lastY = 0, idle = 0;
  stage.addEventListener("pointerdown", (e) => {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("is-dragging");
  });
  stage.addEventListener("pointermove", (e) => {
    const box = stage.getBoundingClientRect();
    nx = ((e.clientX - box.left) / box.width) * 2 - 1;
    ny = ((e.clientY - box.top) / box.height) * 2 - 1;
    hovering = e.pointerType === "mouse";
    if (dragging) {
      targetYaw += (e.clientX - lastX) * 0.008;
      targetPitch = Math.max(-0.5, Math.min(0.6, targetPitch + (e.clientY - lastY) * 0.006));
      lastX = e.clientX; lastY = e.clientY; idle = 0;
    }
  });
  stage.addEventListener("pointerleave", () => { hovering = false; });
  const release = () => { dragging = false; stage.classList.remove("is-dragging"); };
  stage.addEventListener("pointerup", release);
  stage.addEventListener("pointercancel", release);

  let visible = true, last = performance.now(), shown = false, start = null;
  new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; }).observe(stage);
  function frame(now) {
    requestAnimationFrame(frame);
    if (!visible || document.hidden) { last = now; return; }
    // The first frame shows the moment the still render captured.
    start ??= now;
    const t = now - start + OPENING;
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    idle += dt;
    if (!dragging && idle > 2) {
      const wantYaw = hovering ? REST.yaw + nx * 0.4 : REST.yaw + Math.sin(t / 5000) * 0.22;
      const wantPitch = hovering ? REST.pitch + ny * 0.22 : REST.pitch;
      targetYaw += (wantYaw - targetYaw) * dt * 1.5;
      targetPitch += (wantPitch - targetPitch) * dt * 1.5;
    }
    yaw += (targetYaw - yaw) * Math.min(1, dt * 4);
    pitch += (targetPitch - pitch) * Math.min(1, dt * 4);
    update(t, yaw, pitch);
    renderer.render(scene, camera);
    if (!shown) { shown = true; stage.classList.add("is-live"); }
  }
  requestAnimationFrame(frame);
}

const stage = document.querySelector("[data-ntk-hero]");
if (stage && !window.matchMedia("(prefers-reduced-motion: reduce)").matches && webglAvailable()) {
  import("./vendor/three/three.module.js").then(
    (THREE) => start(THREE, stage),
    () => {}, // the still render stays in place
  );
}
