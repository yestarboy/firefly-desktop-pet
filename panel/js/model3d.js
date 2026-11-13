/* ============================================================
   model3d.js —— 对话页的 3D 角色（MMD / PMX）

   为什么单独一个文件：three.js 是 ES Module，只能用 import，
   不能和那堆经典 <script> 混在一起。

   依赖（CDN，见 index.html 的 importmap）：
     three@0.160.0 + examples/jsm/loaders/MMDLoader.js

   模型放这里：
     assets/models3d/firefly.pmx   （贴图放同目录，保持 pmx 里的相对路径）

   没有 .vmd 动作文件，所以待机动作是程序算出来的：
     - 呼吸：上半身轻微起伏
     - 摆头：头部正弦摆动
     - 眨眼：定时触发「まばたき」morph
     - 口型：收到 tts_start / tts_end 时驱动「あ」morph
   ============================================================ */

import * as THREE from "three";
import { MMDLoader } from "three/addons/loaders/MMDLoader.js";

/* ---------- 模型文件：按顺序找，第一个能加载的胜出 ----------
   面板页位于 /panel/index.html，所以这里必须用以根开头的绝对路径，
   否则会被解析成 /panel/assets/models3d/...
   ------------------------------------------------------------ */
const MODEL_CANDIDATES = [
  "/assets/models3d/流萤3.0.pmx",
  "/assets/models3d/firefly.pmx",
];

/* ---------- 可调参数（觉得构图不对就改这里）---------- */
const CAMERA = {
  fov: 28,
  // 画面竖直方向想露出身体的多少：越大 = 拉远
  bodySpan: 0.72,
  // 镜头对准身体的哪个高度：0 = 脚底，1 = 头顶
  lookAtRatio: 0.60,
  // 整体上下微调：正数 = 人往下走
  offsetY: 0.06,
};

/* ---------- 卡通渲染参数 ---------- */
const OUTLINE = {
  on: true,
  color: 0x15171d,
  // 相对模型身高的比例，加载后按包围盒算实际宽度
  ratio: 0.0022,
};

/* ---------- 物理（可选，失败自动退回程序化摆动）---------- */
const PHYSICS = {
  on: true,
};

const IDLE = {
  headSwayY: 0.06,     // 左右摆头（弧度）
  headSwayX: 0.03,     // 点头
  headSpeed: 0.8,
  breathZ: 0.02,       // 上半身侧向起伏
  breathSpeed: 0.9,
};

const MOUTH_MORPHS = ["あ", "い", "う", "え", "お"];
const BLINK_MORPH = "まばたき";

/* ---------- 状态 ---------- */
let renderer = null;
let scene = null;
let camera = null;
let mesh = null;
let clock = null;

let speaking = false;
let blinkUntil = 0;
let nextBlinkAt = 0;

const baseRot = new Map();   // 骨骼基础旋转（在它之上叠加摆动）

/* ---------- 工具 ---------- */

function showTip(text) {
  const el = document.getElementById("chat3dTip");
  if (!el) return;
  el.textContent = text;
  el.hidden = !text;
}

function findBone(name) {
  if (!mesh || !mesh.skeleton) return null;
  return mesh.skeleton.bones.find((b) => b.name === name) || null;
}

function rememberBase(bone) {
  if (bone && !baseRot.has(bone)) {
    baseRot.set(bone, bone.rotation.clone());
  }
}

function morphIndex(name) {
  const dict = mesh && mesh.morphTargetDictionary;
  if (!dict) return -1;
  const i = dict[name];
  return typeof i === "number" ? i : -1;
}

function setMorph(name, value) {
  const i = morphIndex(name);
  if (i >= 0) mesh.morphTargetInfluences[i] = value;
}

function mouth(value) {
  for (const name of MOUTH_MORPHS) {
    setMorph(name, name === "あ" ? value : 0);
  }
}

/* ---------- 场景 ---------- */

function initScene() {
  const canvas = document.getElementById("stage3d");
  if (!canvas) return false;

  renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  // 颜色管理
  // 关键：不要用 ACES。它会把高光和中调压灰、降饱和，
  // 二次元角色需要的是「干脆的分级明暗」，不是电影感。
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.toneMappingExposure = 1.0;

  scene = new THREE.Scene();
  scene.background = null;

  camera = new THREE.PerspectiveCamera(CAMERA.fov, 1, 0.1, 5000);

  // ---- 二次元布光：亮部要亮、暗部要沉，中间不要糊 ----
  // 主光从左上前方来（参考图光源就在左上），轮廓光勾发丝。
  const ambient = new THREE.AmbientLight(0xffffff, 0.35);   // toon 材质需要底光，但多了就糊
  scene.add(ambient);

  const hemi = new THREE.HemisphereLight(0xfff4e8, 0x39404d, 0.35);
  scene.add(hemi);

  const key = new THREE.DirectionalLight(0xfff4e6, 1.25);   // 主光：左上
  key.position.set(-2.2, 3.5, 3.5);
  scene.add(key);

  const fill = new THREE.DirectionalLight(0xcfe0ff, 0.35);  // 补光：右侧
  fill.position.set(3.5, 1.5, 2);
  scene.add(fill);

  const rim = new THREE.DirectionalLight(0xffffff, 0.55);   // 轮廓光：背后
  rim.position.set(0.5, 2.5, -4);
  scene.add(rim);

  clock = new THREE.Clock();
  return true;
}

/** 按包围盒自动取景：把镜头对到胸口高度，只露出上半身 */
function frameHalfBody() {
  if (!mesh || !camera) return;

  const box = new THREE.Box3().setFromObject(mesh);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  const h = size.y || 1;
  const lookY = box.min.y + h * CAMERA.lookAtRatio;

  // 想看到的竖直高度
  const span = h * CAMERA.bodySpan;
  const dist = span / (2 * Math.tan((CAMERA.fov * Math.PI) / 360));

  camera.position.set(
    center.x,
    lookY + h * CAMERA.offsetY,
    box.max.z + dist
  );
  camera.lookAt(center.x, lookY, center.z);
  camera.near = Math.max(0.1, dist * 0.05);
  camera.far = dist * 20;
  camera.updateProjectionMatrix();
}

/* ---------- 姿态：把 T-pose 摆成自然站姿 ---------- */

const BONE_ALIASES = {
  armL: ["左腕", "腕.L", "腕L"],
  armR: ["右腕", "腕.R", "腕R"],
  elbowL: ["左ひじ", "左肘", "ひじ.L"],
  elbowR: ["右ひじ", "右肘", "ひじ.R"],
  wristL: ["左手首", "手首.L"],
  wristR: ["右手首", "手首.R"],
};

function findBoneAny(names) {
  if (!mesh || !mesh.skeleton) return null;
  const bones = mesh.skeleton.bones;
  for (const n of names) {
    const b = bones.find((x) => x.name === n);
    if (b) return b;
  }
  return null;
}

/**
 * 把 bone→child 的朝向重定向到世界方向 targetDir。
 * 用「当前方向 → 目标方向」的四元数直接算，不依赖具体骨骼轴向，
 * 所以换别的模型也不用改。
 */
function aimBone(bone, child, targetDir) {
  if (!bone || !child) return;

  mesh.updateMatrixWorld(true);

  const a = new THREE.Vector3().setFromMatrixPosition(bone.matrixWorld);
  const b = new THREE.Vector3().setFromMatrixPosition(child.matrixWorld);
  const cur = b.clone().sub(a);
  if (cur.lengthSq() < 1e-8) return;
  cur.normalize();

  const target = targetDir.clone().normalize();
  const rot = new THREE.Quaternion().setFromUnitVectors(cur, target);

  const worldQ = new THREE.Quaternion();
  bone.getWorldQuaternion(worldQ);

  const parentQ = new THREE.Quaternion();
  if (bone.parent) bone.parent.getWorldQuaternion(parentQ);

  const newWorldQ = rot.clone().multiply(worldQ);
  bone.quaternion.copy(parentQ.invert().multiply(newWorldQ));

  mesh.updateMatrixWorld(true);
}

function relaxPose() {
  const armL = findBoneAny(BONE_ALIASES.armL);
  const armR = findBoneAny(BONE_ALIASES.armR);
  const elL = findBoneAny(BONE_ALIASES.elbowL);
  const elR = findBoneAny(BONE_ALIASES.elbowR);
  const wrL = findBoneAny(BONE_ALIASES.wristL);
  const wrR = findBoneAny(BONE_ALIASES.wristR);

  if (!armL && !armR) {
    console.warn("[3d] 没找到手臂骨骼，跳过姿态矫正");
    return;
  }

  // 哪边在 +X 由模型自己决定，按实际坐标判断，避免左右反了
  const px = (bone) =>
    bone
      ? Math.sign(new THREE.Vector3().setFromMatrixPosition(bone.matrixWorld).x) || 1
      : 1;

  const sideL = px(armL);
  const sideR = px(armR);

  // 上臂：向下、贴身体、略向前 —— 自然垂手
  aimBone(armL, elL, new THREE.Vector3(sideL * 0.10, -1, 0.08));
  aimBone(armR, elR, new THREE.Vector3(sideR * 0.10, -1, 0.08));

  // 前臂：继续向下，带一点向前的弯（手肘不会绷直）
  aimBone(elL, wrL, new THREE.Vector3(sideL * 0.04, -1, 0.16));
  aimBone(elR, wrR, new THREE.Vector3(sideR * 0.04, -1, 0.16));

  console.log("[3d] 姿态矫正完成：手臂已放下");
}

/* ---------- 头发 / 裙摆：程序化摆动 ---------- */

let swayBones = [];

function initSwayBones() {
  swayBones = [];
  if (!mesh || !mesh.skeleton) return;

  // 没接 ammo 物理，只能按名字挑出"应该会飘"的骨骼自己摆
  const re = /髪|毛|スカート|裾|リボン|尻尾|アホ毛|前髪|後髪|横髪|ツインテ/;

  mesh.skeleton.bones.forEach((b) => {
    if (!re.test(b.name)) return;
    swayBones.push({
      bone: b,
      base: b.quaternion.clone(),
      phase: Math.random() * Math.PI * 2,
      amp: 0.012 + Math.random() * 0.018,
      speed: 1.1 + Math.random() * 0.7,
    });
  });

  console.log("[3d] 会摆动的骨骼:", swayBones.length, swayBones.map((s) => s.bone.name).slice(0, 8).join(","));
}

function applySway(t) {
  for (const s of swayBones) {
    const q = s.base.clone();
    const e = new THREE.Euler(
      Math.sin(t * s.speed + s.phase) * s.amp,
      Math.sin(t * s.speed * 0.7 + s.phase) * s.amp * 0.6,
      Math.sin(t * s.speed * 1.3 + s.phase) * s.amp * 0.8
    );
    q.multiply(new THREE.Quaternion().setFromEuler(e));
    s.bone.quaternion.copy(q);
  }
}

let explicit = false;   // 是否由后端指定了路径（指定后就不再猜候选）

/* ---------- 物理：MMDAnimationHelper + ammo.js ---------- */

let helper = null;
let physicsReady = false;

/**
 * 试着接上 MMD 物理。
 * PMX 自带刚体/关节数据，接上后头发、裙摆是真的受重力和身体动作影响，
 * 而不是我们现在写的假正弦。
 * 任何一步失败都只是退回程序化摆动，不影响出画面。
 */
async function tryInitPhysics(model) {
  if (!PHYSICS.on) return false;

  try {
    // 1) ammo.js（wasm 版，three 放在 addons/libs 下）
    const ammoMod = await import("three/addons/libs/ammo.wasm.js");
    const factory = ammoMod.default || ammoMod.Ammo || ammoMod;
    const AmmoLib = typeof factory === "function" ? await factory() : factory;
    if (!AmmoLib) throw new Error("Ammo 初始化返回空");
    window.Ammo = AmmoLib;

    // 2) MMDAnimationHelper
    const mod = await import("three/addons/animation/MMDAnimationHelper.js");
    const MMDAnimationHelper = mod.MMDAnimationHelper || mod.default;

    helper = new MMDAnimationHelper({ afterglow: 2.0 });
    helper.add(model, { physics: true });

    physicsReady = true;
    console.log("[3d] MMD 物理已启用，头发/裙摆由刚体驱动");
    return true;
  } catch (e) {
    console.warn("[3d] 物理启用失败，退回程序化摆动:", (e && e.message) || e);
    return false;
  }
}

/* ---------- 材质：色彩空间 + 卡通分级 ---------- */

// 4 级明暗台阶。NearestFilter 保证是"硬边"而不是渐变，这才是二次元质感
const TOON_GRADIENT = (() => {
  const t = new THREE.DataTexture(
    new Uint8Array([110, 170, 225, 255]),
    4, 1,
    THREE.RedFormat
  );
  t.minFilter = THREE.NearestFilter;
  t.magFilter = THREE.NearestFilter;
  t.needsUpdate = true;
  return t;
})();

function styleMaterials(root) {
  let maps = 0;
  let toon = 0;

  root.traverse((o) => {
    if (!o.material) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    mats.forEach((m) => {
      // MMD 贴图默认没标色彩空间，当线性数据用会整体提亮发白
      if (m.map && m.map.isTexture) {
        m.map.colorSpace = THREE.SRGBColorSpace;
        m.map.needsUpdate = true;
        maps++;
      }
      // toon 材质换上硬边分级
      if (m.isMeshToonMaterial) {
        m.gradientMap = TOON_GRADIENT;
        toon++;
      }
      // MMD 里很多材质被标成 transparent（贴图带 alpha 通道），
      // 但它们其实是"实心 + 镂空"，不是半透明。
      // 之前的 depthWrite=false 会把整个人变成玻璃，这里改成裁切。
      if (m.transparent) {
        m.depthWrite = true;
        if (m.opacity >= 0.99 && m.map) {
          m.alphaTest = 0.5;
          m.transparent = false;
        }
      }
      m.needsUpdate = true;
    });
  });

  console.log("[3d] 贴图色彩空间:", maps, "张 | toon 材质:", toon, "个");
}

/* ---------- 描边：反向外扩一层，只画背面 ---------- */

function addOutline(root) {
  if (!OUTLINE.on) return;

  const box = new THREE.Box3().setFromObject(root);
  const h = box.getSize(new THREE.Vector3()).y || 10;
  const width = h * OUTLINE.ratio;

  const targets = [];
  root.traverse((o) => {
    if (o.isSkinnedMesh) targets.push(o);
  });

  targets.forEach((src) => {
    // 有透明材质的网格不描边：外壳会把半透明的头发/蕾丝整片盖黑
    const srcMats = Array.isArray(src.material) ? src.material : [src.material];
    if (srcMats.some((m) => m && m.transparent)) {
      console.log("[3d] 跳过透明网格的描边:", src.name || "(unnamed)");
      return;
    }

    const mat = new THREE.MeshToonMaterial({
      color: OUTLINE.color,
      side: THREE.BackSide,   // 只画背面，正面被模型挡住 → 形成外轮廓
    });

    // 往外推一点深度，避免外壳和本体在凹面处抢像素（裙摆变黑就是这个原因）
    mat.polygonOffset = true;
    mat.polygonOffsetFactor = 1;
    mat.polygonOffsetUnits = 1;

    // 顶点沿（蒙皮后的）法线外推，这一层就是"描边壳"
    mat.onBeforeCompile = (shader) => {
      shader.uniforms.outlineWidth = { value: width };
      shader.vertexShader = "uniform float outlineWidth;\n" + shader.vertexShader;
      shader.vertexShader = shader.vertexShader.replace(
        "#include <project_vertex>",
        [
          "vec4 mvPosition = vec4( transformed, 1.0 );",
          "mvPosition = modelViewMatrix * mvPosition;",
          "mvPosition.xyz += normalize( normalMatrix * objectNormal ) * outlineWidth;",
          "gl_Position = projectionMatrix * mvPosition;",
        ].join("\n")
      );
    };

    const outline = new THREE.SkinnedMesh(src.geometry, mat);
    outline.bindMode = src.bindMode;
    outline.bind(src.skeleton, src.bindMatrix);
    outline.frustumCulled = false;
    outline.renderOrder = -1;
    src.parent.add(outline);
  });

  console.log("[3d] 描边网格:", targets.length, "个，宽度", width.toFixed(3));
}

function onLoaded(model, url) {
  mesh = model;
  scene.add(mesh);
  mesh.updateMatrixWorld(true);

  styleMaterials(mesh);

  // 先摆姿势再取景：否则构图是按 T-pose 的宽度算的
  try {
    relaxPose();
  } catch (e) {
    console.warn("[3d] 姿态矫正失败", e);
  }
  mesh.updateMatrixWorld(true);

  initSwayBones();
  frameHalfBody();
  addOutline(mesh);

  // 记录骨骼初始姿态，待机时在它上面叠加摆动
  ["頭", "上半身", "上半身2", "首"].forEach((n) => rememberBase(findBone(n)));

  showTip("");
  const morphs = Object.keys(mesh.morphTargetDictionary || {});
  console.log("[3d] 模型加载完成:", url, "| morph:", morphs.length, "| 骨骼:", (mesh.skeleton ? mesh.skeleton.bones.length : 0));
  console.log("[3d] morph 列表:", morphs.join(","));

  // 物理是异步的（要下 ammo.wasm），成功后再接管头发
  tryInitPhysics(mesh);
}

/** 由后端指定路径后调用（推荐路径） */
function load(url) {
  explicit = true;
  showTip("正在加载模型…");
  const loader = new MMDLoader();
  loader.load(
    url,
    (m) => onLoaded(m, url),
    undefined,
    (err) => {
      console.warn("[3d] 加载失败:", url, (err && (err.message || err)) || err);
      showTip("模型加载失败：详见面板控制台");
    }
  );
}

/** 兜底：按硬编码候选依次尝试 */
function loadCandidates(index) {
  if (index >= MODEL_CANDIDATES.length) {
    showTip("没找到 3D 模型：请把 .pmx 放到 assets/models3d/ 下");
    return;
  }

  const url = MODEL_CANDIDATES[index];
  const loader = new MMDLoader();

  loader.load(
    url,
    (model) => onLoaded(model, url),
    undefined,
    (err) => {
      console.warn("[3d] 候选加载失败:", url, (err && (err.message || err)) || err);
      loadCandidates(index + 1);
    }
  );
}

/* ---------- 动画 ---------- */

function animate() {
  requestAnimationFrame(animate);

  // 用 getDelta 而不是 getElapsedTime：物理引擎要的是"帧间隔"
  // 夹一下上限，避免切页面回来后一大步把物理算炸
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = clock.elapsedTime;

  if (mesh) {
    // 待机：摆头 + 呼吸
    const head = findBone("頭");
    const upper = findBone("上半身");

    if (head) {
      const base = baseRot.get(head);
      if (base) {
        head.rotation.x = base.x + Math.sin(t * IDLE.headSpeed) * IDLE.headSwayX;
        head.rotation.y = base.y + Math.sin(t * IDLE.headSpeed * 0.7) * IDLE.headSwayY;
      }
    }

    if (upper) {
      const base = baseRot.get(upper);
      if (base) {
        upper.rotation.z = base.z + Math.sin(t * IDLE.breathSpeed) * IDLE.breathZ;
      }
    }

    if (physicsReady && helper) {
      // 物理接管头发/裙摆：刚体 + 关节，会跟着头部和身体的运动自然甩
      try {
        helper.update(dt);
      } catch (e) {
        console.warn("[3d] 物理更新失败，退回摆动:", (e && e.message) || e);
        physicsReady = false;
      }
    } else {
      // 没有物理时的兜底：程序化正弦摆动
      applySway(t);
    }

    // 眨眼
    const now = performance.now();
    if (now > nextBlinkAt) {
      blinkUntil = now + 110;
      nextBlinkAt = now + 2500 + Math.random() * 3500;
    }
    setMorph(BLINK_MORPH, now < blinkUntil ? 1 : 0);

    // 口型：说话时用正弦驱动「あ」，停嘴立刻归零
    if (speaking) {
      const v = 0.45 + 0.45 * Math.sin(t * 16);
      mouth(v);
    } else {
      mouth(0);
    }
  }

  renderer.render(scene, camera);
}

function resize() {
  const canvas = document.getElementById("stage3d");
  if (!canvas || !renderer || !camera) return;

  const w = canvas.clientWidth || 1;
  const h = canvas.clientHeight || 1;

  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();

  // 窗口尺寸变了要重新取景，不然构图会跑
  if (mesh) frameHalfBody();
}

/* ---------- 对外接口 ---------- */

function onEvent(event, payload) {
  if (event === "tts_start") {
    speaking = true;
  } else if (event === "tts_end") {
    speaking = false;
    mouth(0);
  }
}

function boot() {
  if (!initScene()) return;

  resize();
  window.addEventListener("resize", resize);
  animate();

  // main.js 拿到 bridge 后会把真实文件名写进 window.MODEL3D_URL；
  // 给它 1.5 秒，拿不到再走硬编码候选。
  if (window.MODEL3D_URL) {
    load(window.MODEL3D_URL);
  } else {
    setTimeout(() => {
      if (!explicit && !mesh) loadCandidates(0);
    }, 1500);
  }
}

window.Model3D = {
  onEvent,
  resize,
  load,
  isReady: () => !!mesh,
  // 调试用：在控制台里看模型到底有哪些 morph 和骨骼
  morphs: () => Object.keys((mesh && mesh.morphTargetDictionary) || {}),
  bones: () => (mesh && mesh.skeleton ? mesh.skeleton.bones.map((b) => b.name) : []),
};

// 模块是 defer 的，DOM 已经就绪
boot();
