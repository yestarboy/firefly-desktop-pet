/* ============================================================
   Firefly 3D 渲染器（Babylon.js + babylon-mmd）
   ------------------------------------------------------------
   为什么用 babylon-mmd 而不是 three.js：
     MMD 的质感 = toon 贴图 + スフィアマップ（球面贴图）+ 描边。
     three 的 MeshToonMaterial 只有第一项，后两项要自己实现；
     babylon-mmd 的 MmdStandardMaterial 是按 MMD 原版实现的，三样都有。

   对外接口（挂到 window.BabylonMMDViewer）：
     init(canvasId)   初始化引擎和场景
     load(url)        加载 PMX
     onEvent(evt,p)   接收 tts_start / tts_end 驱动口型
     resize()         画布尺寸变化时调用
   ============================================================ */

import { Engine } from "@babylonjs/core/Engines/engine";
import { Scene } from "@babylonjs/core/scene";
import { Vector3, Quaternion } from "@babylonjs/core/Maths/math.vector";
import { Color3, Color4 } from "@babylonjs/core/Maths/math.color";
import { ArcRotateCamera } from "@babylonjs/core/Cameras/arcRotateCamera";
import { HemisphericLight } from "@babylonjs/core/Lights/hemisphericLight";
import { DirectionalLight } from "@babylonjs/core/Lights/directionalLight";
import { LoadAssetContainerAsync } from "@babylonjs/core/Loading/sceneLoader";

import { RegisterPmxLoader } from "babylon-mmd/esm/Loader/pmxLoader.pure";
import { RegisterMmdOutlineRenderer } from "babylon-mmd/esm/Loader/mmdOutlineRenderer.pure";
import { MmdStandardMaterialBuilder } from "babylon-mmd/esm/Loader/mmdStandardMaterialBuilder";
import { MmdRuntime } from "babylon-mmd/esm/Runtime/mmdRuntime";
// 物理用单线程实例（SPR）：多线程版（MPR）内含 Web Worker，
// Rollup 会代码分割，IIFE 单文件构建直接失败。
import { MmdWasmInstanceTypeSPR } from "babylon-mmd/esm/Runtime/Optimized/InstanceType/singlePhysicsRelease";
import { GetMmdWasmInstance } from "babylon-mmd/esm/Runtime/Optimized/mmdWasmInstance";
import { MultiPhysicsRuntime } from "babylon-mmd/esm/Runtime/Optimized/Physics/Bind/Impl/multiPhysicsRuntime";
import { MmdBulletPhysics } from "babylon-mmd/esm/Runtime/Optimized/Physics/mmdBulletPhysics";

// 不引入 MmdRuntime / VMD / WASM 物理：
//   1) 物理会把整件衣服/胸腔一起解算，产生抽搐，用户要求「只让头发动」；
//   2) 动作（VMD）功能已按需求移除。
// 头发的动态改由下面的弹簧摆动实现，身体保持完全静止。

RegisterPmxLoader();
RegisterMmdOutlineRenderer();

/* ---------------- 构图（觉得不对就改这里）---------------- */
const VIEW = {
  // 镜头绕 Y 轴角度：-PI/2 = 从 -Z 方向看（MMD 模型的正面通常朝 -Z）
  alpha: -Math.PI / 2,
  beta: Math.PI / 2,
  // 画面竖直方向看到身体的多少：越小 = 越大（越近）
  bodySpan: 0.20,
  // 镜头对准的高度：0 脚底，1 头顶
  lookAtRatio: 0.662,
  // 整体上移/下移（占身高比例，正数＝人往下）
  offsetY: 0.0,
};

/* 描边：babylon-mmd 用 PMX 自带的边缘宽度，普遍偏粗，这里统一收一下 */
const OUTLINE_SCALE = 0.45;

const PHYSICS = {
  // 头发/衣服的刚体物理。关掉就退回纯弹簧摆动。
  on: true,
  // 只保留头发的物理：其它刚体（衣服/裙摆/胸腔）改为跟随骨骼
  hairOnly: true,
};

/* 站立姿态：T 字 → 双手轻放在身前 */
const RELAX = {
  armDrop: 1.12,      // 上臂下压
  armForward: 0.30,   // 上臂前摆
  elbowIn: 0.95,      // 前臂向内收
  elbow: 0.15,        // 手肘微弯
};

const IDLE = {
  // 呼吸：胸腔起伏（物理已移除，不会再和刚体互相顶）
  breathSpeed: 1.05,
  breathUpper: 0.012,     // 上半身前后
  breathChest: 0.008,     // 上半身2
  breathShoulder: 0.010,  // 肩膀随呼吸微抬

  // 重心：慢速左右移动
  swaySpeed: 0.32,
  swayAmount: 0.22,       // センター 横向位移（模型单位）
  swayTilt: 0.014,        // 随之的身体倾斜

  // 头部：幅度小而缓，保持正脸平视
  headSpeed: 0.5,
  headYaw: 0.030,
  headPitch: 0.012,
  headRoll: 0.010,

  // 头部下压（弧度）：负值 = 低头；抵消"抬头俯瞰"的观感，让视线回到水平
  headDown: -0.20,

  // 手臂跟随身体轻摆
  armSway: 0.012,

  // 说话时点头
  talkNod: 0.04,
  talkSpeed: 2.6,
};

const MOUTH_MORPHS = ["あ", "い", "う", "え", "お"];
const BLINK_MORPH = "まばたき";

/* ---------- 表情：情绪名 → 模型 morph ---------- */
const EMOTION_MORPHS = {
  开心: [["笑い", 0.9], ["にこり", 0.35]],
  生气: [["怒り", 0.9]],
  难过: [["困る", 0.8]],
  害羞: [["照れ", 0.9], ["にこり", 0.3]],
  惊讶: [["びっくり", 0.9]],
  平静: [],
};

let currentEmotion = "";

function setEmotion(name) {
  currentEmotion = name || "";
  // 先把所有表情归零，避免叠加
  for (const list of Object.values(EMOTION_MORPHS)) {
    for (const [m] of list) setMorph(m, 0);
  }
  const list = EMOTION_MORPHS[currentEmotion] || [];
  for (const [m, w] of list) setMorph(m, w);
  log("表情:", currentEmotion || "(无)");
}

/* ---------------- 状态 ---------------- */
let engine = null;
let scene = null;
let camera = null;
let rootMesh = null;
let meshes = [];
let skinned = null;
let speaking = false;
let clock = 0;
let blinkNext = 0;
let blinkUntil = 0;

// 动作（VMD）已移除；物理只用于头发/衣服的刚体模拟
let mmdRuntime = null;
let mmdModel = null;
let physicsActive = false;
// 运行时初始化是异步的（要下载 WASM），加载模型前必须等它完成
let runtimePromise = null;
// 骨骼矩阵要等第一帧渲染后才有效，取景推迟到那时再算
let pendingFrame = false;

async function initRuntime() {
  try {
    if (!PHYSICS.on) throw new Error("PHYSICS.on = false");

    const wasmInstance = await GetMmdWasmInstance(new MmdWasmInstanceTypeSPR());

    const physics = new MultiPhysicsRuntime(wasmInstance);
    // MMD 默认 -98（真实值的 10 倍），在浏览器里会把刚体推得来回弹
    physics.setGravity(new Vector3(0, -9.8, 0));
    physics.register(scene);

    mmdRuntime = new MmdRuntime(scene, new MmdBulletPhysics(physics));
    mmdRuntime.loggingEnabled = true;
    mmdRuntime.register(scene);
    mmdRuntime.playAnimation();

    physicsActive = true;
    log("MMD 运行时已启动（含物理）");
    return;
  } catch (e) {
    console.warn("[3d] 物理初始化失败，退回弹簧摆动:", (e && e.message) || e);
    physicsActive = false;
    mmdRuntime = null;
  }
}

/* ---------- 头发 / 裙摆：二级摆动（弹簧，由头部运动驱动） ---------- */

let swayBones = [];
let headPrevYaw = 0;

function initSwayBones() {
  swayBones = [];
  headPrevYaw = 0;
  if (!skinned) return;

  // 只让头发动；衣服（Coat*）和裙摆不参与，身体完全静止
  const re = /hair/i;

  for (const b of skinned.skeleton.bones) {
    if (!re.test(b.name)) continue;
    if (!b.rotationQuaternion) {
      b.rotationQuaternion = Quaternion.FromEulerVector(b.rotation);
    }
    swayBones.push({
      bone: b,
      baseQ: b.rotationQuaternion.clone(),
      angle: 0,
      vel: 0,
      phase: Math.random() * Math.PI * 2,
      amp: 0.008 + Math.random() * 0.012,
      speed: 1.0 + Math.random() * 0.8,
    });
  }

  log("二级摆动骨骼:", swayBones.length);

  // 调试：把全部骨名打出来，方便确认头发/裙摆叫什么
  try {
    const names = skinned.skeleton.bones.map((b) => b.name);
    log("BONES(" + names.length + "):", names.join(","));
  } catch (e) {}
}

function applySway(t) {
  if (!swayBones.length) return;

  // 用头部转动的速度去"甩"头发：头动得越快，头发甩得越明显
  const head = findBone("頭");
  let yaw = 0;
  if (head && head.rotationQuaternion) {
    yaw = head.rotationQuaternion.toEulerAngles().y;
  }
  const dv = yaw - headPrevYaw;
  headPrevYaw = yaw;

  for (const s of swayBones) {
    // 弹簧：被甩一下 → 回弹 → 阻尼收敛
    s.vel += (dv * 26 - s.angle * 7) * 0.08;
    s.vel *= 0.86;
    s.angle += s.vel;

    // 再叠一点微风般的持续轻摆
    const flutter = Math.sin(t * s.speed + s.phase) * s.amp;
    const q = s.baseQ.multiply(
      Quaternion.RotationAxis(new Vector3(1, 0, 0), s.angle + flutter)
    );
    s.bone.rotationQuaternion = q;
  }
}

function log(...args) {
  console.log("[3d]", ...args);
}

function findBone(name) {
  if (!skinned || !skinned.skeleton) return null;
  return skinned.skeleton.bones.find((b) => b.name === name) || null;
}

function setMorph(name, value) {
  for (const m of meshes) {
    const mgr = m.morphTargetManager;
    if (!mgr) continue;
    const target = mgr.getTargetByName(name);
    if (target) target.influence = value;
  }
}

function mouth(v) {
  for (const name of MOUTH_MORPHS) setMorph(name, name === "あ" ? v : 0);
}

/* ---------------- 姿态：T-pose → 自然垂手 ---------------- */

const BONE_ALIASES = {
  armL: ["左腕", "腕.L", "腕L"],
  armR: ["右腕", "腕.R", "腕R"],
  elbowL: ["左ひじ", "左肘", "ひじ.L"],
  elbowR: ["右ひじ", "右肘", "ひじ.R"],
  wristL: ["左手首", "手首.L"],
  wristR: ["右手首", "手首.R"],
};

function findBoneAny(names) {
  for (const n of names) {
    const b = findBone(n);
    if (b) return b;
  }
  return null;
}

/**
 * 把 bone→child 的朝向重定向到世界方向 targetDir。
 * 用「当前方向 → 目标方向」的轴角算，不依赖骨骼轴向，换模型也不用改。
 */
function aimBone(bone, child, targetDir) {
  if (!bone || !child || !skinned) return;

  skinned.skeleton.computeAbsoluteTransforms();

  const a = bone.getAbsolutePosition().clone();
  const b = child.getAbsolutePosition().clone();
  const cur = b.subtract(a);
  if (cur.lengthSquared() < 1e-8) return;
  cur.normalize();

  const target = targetDir.clone().normalize();
  const dot = Math.min(1, Math.max(-1, Vector3.Dot(cur, target)));
  const angle = Math.acos(dot);
  if (angle < 1e-4) return;

  const axis = Vector3.Cross(cur, target);
  if (axis.lengthSquared() < 1e-8) return;
  axis.normalize();

  const rotWorld = Quaternion.RotationAxis(axis, angle);

  const worldQ = bone.getAbsoluteTransform().getRotationQuaternion().clone();
  const newWorld = rotWorld.multiply(worldQ);

  const parent = bone.getParent();
  const parentQ = parent
    ? parent.getAbsoluteTransform().getRotationQuaternion().clone()
    : Quaternion.Identity();

  bone.setRotationQuaternion(Quaternion.Inverse(parentQ).multiply(newWorld));

  skinned.skeleton.computeAbsoluteTransforms();
}

function relaxPose() {
  const armL = findBoneAny(BONE_ALIASES.armL);
  const armR = findBoneAny(BONE_ALIASES.armR);

  if (!armL && !armR) {
    console.warn("[3d] 没找到手臂骨骼，保持原姿态");
    return;
  }

  // MMD 模型在绑定姿态下手臂沿 ±X 伸出（T 字）。
  // 直接给骨骼设局部旋转把它压下来；左右符号相反。
  const drop = RELAX.armDrop;

  const Z = new Vector3(0, 0, 1);
  const Y = new Vector3(0, 1, 0);
  const X = new Vector3(1, 0, 0);

  // 上臂：先下压，再向前摆
  const armQuat = (sideSign) => {
    let q = Quaternion.RotationAxis(Z, sideSign * drop);
    q = q.multiply(Quaternion.RotationAxis(Y, -sideSign * RELAX.armForward));
    return q;
  };

  if (armL) armL.setRotationQuaternion(armQuat(-1));
  if (armR) armR.setRotationQuaternion(armQuat(1));

  // 前臂：向内收，让双手交叠到身前
  const elL = findBoneAny(BONE_ALIASES.elbowL);
  const elR = findBoneAny(BONE_ALIASES.elbowR);

  // 前臂：绕 X 轴弯（MMD 的 ひじ 是这种弯曲方式），让手到身前
  const elbowQuat = (sideSign) => {
    let q = Quaternion.RotationAxis(X, RELAX.elbowIn);
    q = q.multiply(Quaternion.RotationAxis(Z, sideSign * 0.12));
    return q;
  };

  if (elL) elL.setRotationQuaternion(elbowQuat(1));
  if (elR) elR.setRotationQuaternion(elbowQuat(-1));

  if (skinned) skinned.skeleton.computeAbsoluteTransforms();

  log("姿态矫正：双手放到身前，角度", drop);
}

/* ---------------- 待机动画：骨骼基础姿态 + 每帧叠加 ---------------- */

// 参与待机的骨骼：{ name, bone, baseQ, basePos }
let animBones = [];

function collectAnimBones() {
  animBones = [];

  const names = [
    "センター", "上半身", "上半身2", "首", "頭",
    "左肩", "右肩", "左腕", "右腕",
  ];

  for (const n of names) {
    const b = findBone(n);
    if (!b) continue;

    // 统一用四元数，避免 rotation / rotationQuaternion 混用
    if (!b.rotationQuaternion) {
      b.rotationQuaternion = Quaternion.FromEulerVector(b.rotation);
    }

    animBones.push({
      name: n,
      bone: b,
      baseQ: b.rotationQuaternion.clone(),
      basePos: b.position.clone(),
    });
  }

  log("待机骨骼:", animBones.map((a) => a.name).join(",") || "(无)");
}

function animOf(name) {
  return animBones.find((a) => a.name === name);
}

function addRot(entry, x, y, z) {
  if (!entry) return;
  const delta = Quaternion.FromEulerAngles(x, y, z);
  entry.bone.rotationQuaternion = entry.baseQ.multiply(delta);
}

function applyIdle(t) {
  const breath = Math.sin(t * IDLE.breathSpeed);
  const sway = Math.sin(t * IDLE.swaySpeed);
  const swaySlow = Math.sin(t * IDLE.swaySpeed * 0.5);
  const headNoise = Math.sin(t * IDLE.headSpeed) * 0.6 + Math.sin(t * 0.37) * 0.4;

  // 重心：整体左右平移 + 身体轻微倾斜
  const center = animOf("センター");
  if (center) {
    center.bone.position.x = center.basePos.x + sway * IDLE.swayAmount;
    center.bone.position.z = center.basePos.z + swaySlow * 0.05;
  }

  // 上半身：呼吸（前后）+ 随重心倾斜
  // 物理开启时幅度收窄：胸腔有刚体，两边同时用力会互相顶出抖动
  const bodyScale = physicsActive ? 0.4 : 1;
  addRot(animOf("上半身"), breath * IDLE.breathUpper * bodyScale, 0, sway * IDLE.swayTilt * bodyScale);
  addRot(animOf("上半身2"), breath * IDLE.breathChest * bodyScale, 0, 0);

  // 肩膀随呼吸微抬（左右反向，看起来像吸气）
  addRot(animOf("左肩"), 0, 0, breath * IDLE.breathShoulder * bodyScale);
  addRot(animOf("右肩"), 0, 0, -breath * IDLE.breathShoulder * bodyScale);

  // 颈部轻微反向，保持头部水平；headDown 是"低头"补偿，抵消抬头的观感
  addRot(animOf("首"), IDLE.headDown, headNoise * 0.02, -sway * IDLE.swayTilt * 0.6);

  // 头：慢速左右看 + 轻微点头；说话时加点动
  const nod = speaking ? Math.sin(t * IDLE.talkSpeed) * IDLE.talkNod : 0;
  addRot(
    animOf("頭"),
    headNoise * IDLE.headPitch + nod + IDLE.headDown * 0.5,
    headNoise * IDLE.headYaw,
    headNoise * IDLE.headRoll
  );

  // 手臂：跟随身体轻摆
  addRot(animOf("左腕"), 0, 0, -sway * IDLE.armSway);
  addRot(animOf("右腕"), 0, 0, sway * IDLE.armSway);
}

/* ---------------- 描边宽度 ---------------- */

function tuneOutline() {
  let n = 0;
  let hairMats = 0;
  const names = [];

  for (const m of meshes) {
    const mat = m.material;
    if (!mat) continue;

    const matName = String(mat.name || "");
    names.push(matName);

    if (typeof mat.outlineWidth === "number" && mat.outlineWidth > 0) {
      mat.outlineWidth *= OUTLINE_SCALE;
      n++;
    }

    // 材质名可能是中文（头发/脸/眼睛）也可能英文（Hair）
    const isHair = /hair|髪|毛|头发|髪の毛/i.test(matName);

    try {
      if (isHair) {
        // 头发"抛光感"的来源：高光 + 球面贴图（スフィアマップ）。
        // 这两样一起去掉，头发就变成哑光的卡通质感。
        if (mat.specularColor && typeof mat.specularColor.set === "function") {
          mat.specularColor.set(0, 0, 0);
        }
        if ("sphereTexture" in mat) {
          mat.sphereTexture = null;
        }
        hairMats++;
      } else if (mat.specularColor && typeof mat.specularColor.scaleInPlace === "function") {
        // 其它部位稍微压一点高光，别整体发亮
        mat.specularColor.scaleInPlace(0.35);
      }
    } catch (e) {
      // 材质类型不同就跳过
    }

    mat.needsUpdate = true;
  }

  log("描边收敛:", n, "个 | 头发材质去高光:", hairMats, "个");
  log("材质名:", names.slice(0, 40).join(" / "));
}

/* ---------------- 初始化 ---------------- */

function init(canvasId) {
  if (engine) return true;

  const canvas = document.getElementById(canvasId);
  if (!canvas) {
    console.warn("[3d] 找不到 canvas:", canvasId);
    return false;
  }

  engine = new Engine(canvas, true, { preserveDrawingBuffer: true, stencil: true }, true);

  // 不要 babylon 自带的加载遮罩（会盖住整个画布）
  engine.displayLoadingUI = () => {};

  scene = new Scene(engine);
  scene.clearColor = new Color4(0, 0, 0, 0);   // 透明背景，透出面板底色
  scene.ambientColor = new Color3(0.5, 0.5, 0.5);

  camera = new ArcRotateCamera("cam", VIEW.alpha, VIEW.beta, 30, new Vector3(0, 10, 0), scene);
  camera.fov = 0.55;
  camera.minZ = 0.1;
  camera.maxZ = 5000;
  camera.inputs.clear();      // 不要鼠标交互，画布是穿透的

  const hemi = new HemisphericLight("hemi", new Vector3(0, 1, 0), scene);
  hemi.intensity = 0.55;

  const dir = new DirectionalLight("dir", new Vector3(0.4, -1, 1), scene);
  dir.intensity = 1.1;

  const rim = new DirectionalLight("rim", new Vector3(-0.3, -0.4, -1), scene);
  rim.intensity = 0.45;

  const renderFrame = () => {
    // 夹一下帧间隔，切走再回来时不会一大步把物理算炸
    clock += Math.min(engine.getDeltaTime() / 1000, 0.05);
    scene.render();
  };

  engine.runRenderLoop(renderFrame);

  // 面板不可见时停掉渲染循环：WebEngine 里 3D 最吃 CPU/GPU，
  // 窗口切走或最小化后还满帧跑纯属浪费（也占内存带宽）
  document.addEventListener("visibilitychange", function () {
    try {
      if (document.hidden) {
        engine.stopRenderLoop(renderFrame);
      } else {
        engine.stopRenderLoop(renderFrame);
        engine.runRenderLoop(renderFrame);
      }
    } catch (e) {
      /* 忽略 */
    }
  });

  // 关键：在"动画步之后、渲染之前"写骨骼。
  // MmdRuntime 每帧会重算骨骼，早于它写就等于白写。
  scene.onAfterAnimationsObservable.add(updateFrame);

  // 物理是异步的（要下载 WASM），这里只启动，失败也不影响出画面
  runtimePromise = initRuntime();

  window.addEventListener("resize", resize);

  log("引擎就绪");
  return true;
}

function resize() {
  if (engine) engine.resize();
}

/* ---------------- 加载模型 ---------------- */

async function load(url) {
  if (!scene) {
    console.warn("[3d] 场景未初始化");
    return;
  }

  try {
    log("开始加载:", url);

    // 物理运行时是异步初始化的，模型必须等它准备好再接入
    if (runtimePromise) {
      try {
        await runtimePromise;
      } catch (e) {
        console.warn("[3d] 等待物理运行时失败:", (e && e.message) || e);
      }
    }

    const container = await LoadAssetContainerAsync(url, scene, {
      pluginOptions: {
        mmdmodel: {
          loggingEnabled: true,
          materialBuilder: new MmdStandardMaterialBuilder(),
        },
      },
    });

    container.addAllToScene();

    rootMesh = container.rootNodes[0];
    meshes = container.meshes.filter((m) => m.getTotalVertices() > 0);
    skinned = meshes.find((m) => m.skeleton) || null;

    tuneOutline();

    // 诊断：看看 PMX 里的刚体都绑在哪些骨骼上、有哪些字段
    try {
      const rb = rootMesh.metadata && rootMesh.metadata.rigidBodies;
      const bones = (rootMesh.metadata && rootMesh.metadata.bones) || [];
      if (rb && rb.length) {
        log("刚体数:", rb.length, "| 字段:", Object.keys(rb[0]).join(","));
      }
    } catch (e) {
      /* 诊断失败不影响加载 */
    }

    // 只保留头发的物理：把绑在其它骨骼上的刚体改成"跟随骨骼"模式，
    // 这样衣服/裙摆/胸腔就不会再被物理解算（也就不会抽搐）。
    if (PHYSICS.hairOnly) {
      try {
        const rb = rootMesh.metadata && rootMesh.metadata.rigidBodies;
        const bones = (rootMesh.metadata && rootMesh.metadata.bones) || [];
        let disabled = 0;
        let kept = 0;

        for (const body of rb || []) {
          const bone = bones[body.boneIndex];
          const name = (bone && bone.name) || "";
          const isHair = /hair/i.test(name);

          if (isHair) {
            kept++;
            continue;
          }

          // PMX 刚体的 mode：0 = 跟随骨骼（不参与物理）
          if (typeof body.mode === "number") body.mode = 0;
          if (typeof body.physicsMode === "number") body.physicsMode = 0;
          disabled++;
        }

        log("物理只保留头发：头发刚体", kept, "个，其余", disabled, "个改为跟随骨骼");
      } catch (e) {
        console.warn("[3d] 物理裁剪失败:", (e && e.message) || e);
      }
    }

    // 先摆姿势，再让物理接管：顺序反了刚体会被瞬间拉扯、抽搐好几秒
    if (skinned) {
      try {
        relaxPose();
      } catch (e) {
        console.warn("[3d] 姿态矫正失败", e);
      }
    }

    if (mmdRuntime) {
      try {
        mmdModel = mmdRuntime.createMmdModel(rootMesh);
        log("模型已接入物理运行时");
      } catch (e) {
        console.warn("[3d] createMmdModel 失败:", (e && e.message) || e);
      }
    }

    frameHalfBody();
    pendingFrame = true;   // 第一帧骨骼矩阵生效后再算一次精确取景
    collectAnimBones();
    initSwayBones();

    log("模型加载完成，网格:", meshes.length, "骨骼:", skinned ? skinned.skeleton.bones.length : 0);
  } catch (e) {
    console.error("[3d] 加载失败:", (e && e.message) || e);
  }
}

/** 半身取景：算包围盒，把镜头对到胸口，只露出上半身 */
function frameHalfBody() {
  if (!rootMesh || !camera) return;

  // 包围盒会把不可见的辅助网格/骨骼也算进去，导致按它取景时人显得很小。
  // 所以优先用骨骼量真实身高：头骨 → 脚骨。
  const bounds = rootMesh.getHierarchyBoundingVectors(true);
  let minY = bounds.min.y;
  let maxY = bounds.max.y;
  let source = "包围盒";

  try {
    if (skinned) {
      skinned.skeleton.computeAbsoluteTransforms();

      // 用整副骨架的 Y 范围量身高：不依赖具体骨名，也不会被
      // 不可见的辅助网格撑大（PMX 里常有这种网格，会把包围盒拉高）
      let boneMin = Infinity;
      let boneMax = -Infinity;

      for (const b of skinned.skeleton.bones) {
        const y = b.getAbsolutePosition().y;
        if (!isFinite(y)) continue;
        if (y < boneMin) boneMin = y;
        if (y > boneMax) boneMax = y;
      }

      const bodyH = boneMax - boneMin;
      if (isFinite(bodyH) && bodyH > 1) {
        // 头骨在颈部，颅顶还要再往上一点
        minY = boneMin;
        maxY = boneMin + bodyH * 1.16;
        source = "骨架";
      }
    }
  } catch (e) {
    console.warn("[3d] 骨架量身高失败，退回包围盒:", (e && e.message) || e);
  }

  const h = maxY - minY || 20;
  const lookY = minY + h * VIEW.lookAtRatio;
  const span = h * VIEW.bodySpan;

  // 由竖直可视范围反推距离：span = 2 * d * tan(fov/2)
  const dist = span / (2 * Math.tan(camera.fov / 2));

  camera.setTarget(new Vector3(0, lookY + h * VIEW.offsetY, 0));
  camera.radius = dist;

  log(
    "取景: 身高", h.toFixed(1),
    "(" + source + ")",
    "距离", dist.toFixed(1),
    "看向", (lookY + h * VIEW.offsetY).toFixed(1),
    "| 包围盒高", (bounds.max.y - bounds.min.y).toFixed(1)
  );
}

/* ---------------- 每帧 ---------------- */

function updateFrame() {
  if (!scene) return;

  // 呼吸/待机永远保留；头发的动态优先交给物理，
  // 物理没起来时用弹簧摆动兜底
  if (skinned) {
    applyIdle(clock);
    if (!physicsActive) applySway(clock);
  }

  // 第一帧之后骨骼矩阵才是真的，这时重新取景才准
  if (pendingFrame && skinned) {
    pendingFrame = false;
    frameHalfBody();
  }

  // 眨眼
  const now = performance.now();
  if (now > blinkNext) {
    blinkUntil = now + 110;
    blinkNext = now + 2500 + Math.random() * 3500;
  }
  setMorph(BLINK_MORPH, now < blinkUntil ? 1 : 0);

  // 口型：幅度收小。有些模型的「あ」形变很猛，
  // 推满（0.7）会让脸看起来像崩掉，0.36 上限自然得多。
  mouth(speaking ? 0.18 + 0.18 * Math.sin(clock * 16) : 0);
}

/* ---------------- 事件 ---------------- */

function onEvent(evt, payload) {
  if (evt === "tts_start") {
    speaking = true;
  } else if (evt === "tts_end") {
    speaking = false;
    mouth(0);
    // 一句话说完了就把表情收回默认，保持"平时无表情"
    setTimeout(function () {
      if (!speaking) setEmotion("");
    }, 1500);
  } else if (evt === "pet_motion") {
    // 后端解析出（开心）这类情绪标签后广播过来
    const name = payload && (payload.name || payload.motion);
    if (name) setEmotion(name);
  }
}

window.BabylonMMDViewer = {
  init,
  load,
  onEvent,
  resize,
  isReady: () => !!rootMesh,
};
console.log("[3d] BUILD-2026-10-30-A | bodySpan=", VIEW.bodySpan, "| outlineScale=", OUTLINE_SCALE);
