// The 3D logo on the documentation's front page: the owl guarding a document. The still poster
// shows first; the model loads once the page is idle and cross-fades in after its first frame.
// Every ten seconds a grant cycle lifts the redaction bar, and every third cycle denies it.
// Motion stops for prefers-reduced-motion, in hidden tabs and when the logo is off screen; any
// failure leaves the poster in place. three.js is vendored in vendor/three (see its README).

import * as THREE from "three";
import { DRACOLoader } from "./vendor/three/examples/jsm/loaders/DRACOLoader.js";
import { GLTFLoader } from "./vendor/three/examples/jsm/loaders/GLTFLoader.js";

const MODEL = new URL("brand/wardowl.glb", import.meta.url).href;
const DRACO = new URL("vendor/three/examples/jsm/libs/draco/gltf/", import.meta.url).href;

const stage = document.querySelector("[data-wo-hero]");
const canvas = stage && stage.querySelector("canvas");
const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const fine = matchMedia("(hover: hover) and (pointer: fine)");
const rad = THREE.MathUtils.degToRad;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const smooth = (t) => ((t = clamp(t, 0, 1)), t * t * (3 - 2 * t));
const out = (t) => 1 - Math.pow(1 - clamp(t, 0, 1), 3);

let renderer, scene, camera, model, head, body, documentMesh, docBase, redactionBar, warmth;
let lids = [], wings = [];
let ready = false, visible = false, raf = 0, last = null, time = 0, manualYaw = 0;
let nextBlink = 4 + Math.random() * 2, blinkStart = -2, doubleBlink = false;
let dragging = false, dragX = 0, dragId = null, dragYaw = 0;
const target = { x: 0, y: 0 }, pointer = { x: 0, y: 0 };
const documentMaterials = [], redactionMaterials = [];

const active = () => ready && visible && !document.hidden && !reduced.matches;
const draw = () => ready && renderer.render(scene, camera);
const resetPointer = () => { target.x = target.y = 0; dragging = false; };

function blinkValue(t) {
  if (t < 0 || t >= 0.19) return 0;
  return t < 0.06 ? t / 0.06 : t < 0.1 ? 1 : 1 - (t - 0.1) / 0.09;
}

// The owl looks left, then right, then back, in the first half of each ten-second cycle.
function headAngle(t) {
  if (t < 2) return 0;
  if (t < 2.28) return -12 * out((t - 2) / 0.28);
  if (t < 3.48) return -12;
  if (t < 3.88) return -12 + 24 * out((t - 3.48) / 0.4);
  if (t < 4.88) return 12;
  if (t < 5.23) return 12 * (1 - out((t - 4.88) / 0.35));
  return 0;
}

// Seconds 7 to 8.4 of a cycle: the bar slides away and the revealed line warms.
function accessState(t) {
  if (t < 7 || t >= 8.4) return { lift: 0, warm: 0, glint: 0 };
  const u = t - 7;
  const lift = u < 0.5 ? smooth(u / 0.5) : u < 0.8 ? 1 : 1 - smooth((u - 0.8) / 0.6);
  const warm = u < 0.4 ? smooth(u / 0.4) : u < 0.8 ? 1 : 1 - smooth((u - 0.8) / 0.6);
  return { lift, warm, glint: u < 0.6 ? Math.sin((Math.PI * u) / 0.6) : 0 };
}

function applyPose(still = false) {
  if (!ready) return;
  const t = still ? 0 : time % 10;
  const enter = still ? 1 : out(time / 1.2);
  const denied = !still && Math.floor(time / 10) % 3 === 2;
  const a = still || denied ? { lift: 0, warm: 0, glint: 0 } : accessState(t);
  model.scale.setScalar(0.96 + 0.04 * enter);
  model.position.y = still ? 0 : -0.08 * (1 - enter);
  model.rotation.set(still ? 0 : rad(pointer.y * 6), manualYaw + (still ? 0 : rad(pointer.x * 8)), 0);
  head.rotation.y = still ? 0 : rad(headAngle(t) + pointer.x * 5);
  if (denied && t >= 7 && t < 8.2) {
    head.rotation.y += rad(6 * Math.sin(((t - 7) * Math.PI * 4) / 0.8) * Math.sin((Math.PI * (t - 7)) / 1.2));
  }
  head.rotation.x = rad(12 * a.lift);
  head.rotation.z = still ? 0 : rad(Math.sin(time * 0.65) * 0.15);
  body.scale.y = still ? 1 : 1 + 0.006 * (1 - Math.cos((time * Math.PI * 2) / 4.5));
  const b = still ? 0 : Math.max(blinkValue(time - blinkStart), doubleBlink ? blinkValue(time - blinkStart - 0.3) : 0);
  for (const lid of lids) {
    const axis = lid.userData.blinkAxis
      || (lid.userData.blinkAxis = ["x", "y", "z"].reduce((m, c) => (lid.scale[c] < lid.scale[m] ? c : m), "x"));
    lid.scale.set(1, 1, 1);
    lid.scale[axis] = Math.max(0.001, b);
    lid.visible = b > 0.001;
  }
  documentMesh.position.copy(docBase);
  documentMesh.rotation.set(0, 0, 0);
  if (redactionBar) redactionBar.position.x = 0.83 * a.lift;
  for (const m of redactionMaterials) m.opacity = 1 - smooth((a.lift - 0.15) / 0.45);
  wings.forEach((wing, i) => {
    wing.rotation.z = rad((i === 0 ? -1 : 1) * (denied && t >= 7 && t < 8.4 ? -4 : 6 * a.lift));
  });
  for (const m of documentMaterials) m.emissiveIntensity = a.warm * 0.35;
  warmth.intensity = a.glint;
  warmth.position.set(0.05, 1.7 - ((t - 7) / 0.6) * 0.9, 1.15);
}

function tick(now) {
  raf = 0;
  if (!active()) { last = null; return; }
  const dt = last === null ? 0 : (now - last) / 1000;
  last = now;
  time += dt;
  if (time >= nextBlink) {
    blinkStart = time;
    doubleBlink = Math.random() < 0.25;
    nextBlink = time + 4 + Math.random() * 2;
  }
  const s = 1 - Math.exp(-dt / 0.14);
  pointer.x += (target.x - pointer.x) * s;
  pointer.y += (target.y - pointer.y) * s;
  applyPose();
  draw();
  raf = requestAnimationFrame(tick);
}

function sync() {
  if (active()) { if (!raf) raf = requestAnimationFrame(tick); return; }
  cancelAnimationFrame(raf);
  raf = 0;
  last = null;
  if (ready && reduced.matches) { applyPose(true); draw(); }
}

function resize() {
  if (!renderer) return;
  const r = stage.getBoundingClientRect();
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setSize(r.width, r.height, false);
  draw();
}

// A small studio environment for the enamel and gold, made here rather than downloaded.
function environment() {
  const w = 256, h = 128, pixels = new Uint8Array(w * h * 4);
  const boxes = [[-1, 0.7, 1, 1.8, 0.1], [1, 0.4, 0.6, 1.3, 0.07], [0, 1, 0, 0.9, 0.14]];
  const lights = boxes.map(([x, y, z, power, width]) => ({ v: new THREE.Vector3(x, y, z).normalize(), power, width }));
  const v = new THREE.Vector3();
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const theta = (x / w - 0.5) * Math.PI * 2, phi = (y / h) * Math.PI;
      v.set(Math.sin(phi) * Math.cos(theta), Math.cos(phi), Math.sin(phi) * Math.sin(theta));
      let light = 0.15 + 0.09 * Math.max(0, v.y);
      for (const l of lights) light += l.power * Math.exp((v.dot(l.v) - 1) / l.width);
      const i = (y * w + x) * 4;
      pixels[i] = clamp(light * 230, 0, 255);
      pixels[i + 1] = clamp(light * 237, 0, 255);
      pixels[i + 2] = clamp(light * 255, 0, 255);
      pixels[i + 3] = 255;
    }
  }
  const texture = new THREE.DataTexture(pixels, w, h, THREE.RGBAFormat);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.mapping = THREE.EquirectangularReflectionMapping;
  texture.needsUpdate = true;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const result = pmrem.fromEquirectangular(texture);
  texture.dispose();
  pmrem.dispose();
  return result.texture;
}

async function init() {
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "low-power" });
    renderer.setClearColor(0, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.NoToneMapping;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    scene = new THREE.Scene();
    scene.environment = environment();
    scene.environmentIntensity = 0.8;
    camera = new THREE.OrthographicCamera(-2.05, 2.05, 2.05, -2.05, 0.1, 40);
    camera.position.set(0, 1.75, 8);
    camera.lookAt(0, 1.75, 0);
    scene.add(new THREE.HemisphereLight(0xd9e5ff, 0x7c6747, 0.85));
    const key = new THREE.DirectionalLight(0xfff0d8, 2.3);
    key.position.set(-3, 5, 4);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    Object.assign(key.shadow.camera, { left: -2, right: 2, top: 3.5, bottom: -1 });
    key.shadow.normalBias = 0.015;
    key.shadow.bias = -0.00015;
    key.shadow.radius = 4;
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xbfd3ff, 1.1);
    fill.position.set(3, 2, 3);
    scene.add(fill);
    const rim = new THREE.DirectionalLight(0xffe0a4, 1.7);
    rim.position.set(2, 4, -3);
    scene.add(rim);
    warmth = new THREE.PointLight(0xffbd57, 0, 4, 2);
    scene.add(warmth);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.18 }));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);

    // The JavaScript decoder, not the WebAssembly one, so the repository holds no binary files.
    const draco = new DRACOLoader().setDecoderPath(DRACO).setDecoderConfig({ type: "js" });
    const gltf = await new GLTFLoader().setDRACOLoader(draco).loadAsync(MODEL);
    draco.dispose();
    model = gltf.scene;
    scene.add(model);
    floor.position.y = new THREE.Box3().setFromObject(model).min.y + 0.001;
    head = model.getObjectByName("Head");
    body = model.getObjectByName("Body");
    documentMesh = model.getObjectByName("Document");
    redactionBar = model.getObjectByName("RedactionBar");
    wings = ["Wing_L", "Wing_R"].map((n) => model.getObjectByName(n)).filter(Boolean);
    lids = ["Lid_L", "Lid_R"].map((n) => model.getObjectByName(n));
    if (!head || !body || !documentMesh || lids.some((l) => !l)) throw new Error("the model's animation controls are missing");
    docBase = documentMesh.position.clone();
    model.traverse((ob) => {
      if (!ob.isMesh) return;
      ob.castShadow = ob.receiveShadow = true;
      if (ob.name === "Document_line_3") {
        ob.material = ob.material.clone();
        ob.material.emissive.set(0xd4a84b);
        ob.material.emissiveIntensity = 0;
        documentMaterials.push(ob.material);
      }
      if (ob.name.startsWith("RedactionBar")) {
        const fade = (mat) => { const m = mat.clone(); m.transparent = true; redactionMaterials.push(m); return m; };
        ob.material = Array.isArray(ob.material) ? ob.material.map(fade) : fade(ob.material);
      }
    });
    ready = true;
    resize();
    applyPose(reduced.matches);
    draw();
    stage.classList.add("is-live");
    sync();
  } catch (error) {
    console.warn("wardowl: the 3D logo is unavailable, so the still image stays.", error);
    ready = false;
    stage.classList.remove("is-live");
  }
}

function start() {
  canvas.addEventListener("pointerdown", (e) => {
    if (!ready || !fine.matches || e.pointerType !== "mouse") return;
    dragging = true;
    dragId = e.pointerId;
    dragX = e.clientX;
    dragYaw = manualYaw;
    canvas.setPointerCapture(e.pointerId);
    stage.classList.add("is-dragging");
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!ready || !fine.matches || e.pointerType !== "mouse") return;
    if (dragging) {
      manualYaw = dragYaw + (e.clientX - dragX) * 0.009;
      if (reduced.matches) { applyPose(true); draw(); }
      return;
    }
    if (reduced.matches) return;
    const b = stage.getBoundingClientRect();
    target.x = clamp(((e.clientX - b.left) / b.width) * 2 - 1, -1, 1);
    target.y = clamp(((e.clientY - b.top) / b.height) * 2 - 1, -1, 1);
  });
  const release = () => {
    dragging = false;
    stage.classList.remove("is-dragging");
    if (dragId !== null && canvas.hasPointerCapture(dragId)) canvas.releasePointerCapture(dragId);
    dragId = null;
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", () => { release(); resetPointer(); });
  canvas.addEventListener("pointerleave", () => { if (!dragging) resetPointer(); });
  stage.addEventListener("keydown", (e) => {
    if (!ready || !["ArrowLeft", "ArrowRight", "Escape"].includes(e.key)) return;
    e.preventDefault();
    manualYaw = e.key === "Escape" ? 0 : manualYaw + (e.key === "ArrowLeft" ? -0.18 : 0.18);
    if (reduced.matches) { applyPose(true); draw(); }
  });
  canvas.addEventListener("webglcontextlost", (e) => {
    e.preventDefault();
    ready = false;
    stage.classList.remove("is-live");
    sync();
  });
  document.addEventListener("visibilitychange", () => { resetPointer(); sync(); });
  window.addEventListener("blur", resetPointer);
  reduced.addEventListener("change", () => { time = 0; last = null; applyPose(reduced.matches); draw(); sync(); });
  new IntersectionObserver((entries) => {
    visible = entries[0].isIntersecting;
    if (!visible) resetPointer();
    sync();
  }).observe(stage);
  new ResizeObserver(resize).observe(stage);
  init();
}

// Only browsers with WebGL get the model; the rest, and phones that save data, keep the poster.
const saveData = navigator.connection && navigator.connection.saveData;
if (stage && canvas && !saveData && "IntersectionObserver" in window && "ResizeObserver" in window) {
  const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 300));
  idle(start, { timeout: 2000 });
}
