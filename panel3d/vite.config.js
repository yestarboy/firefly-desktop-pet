import { defineConfig } from "vite";

// 输出单个 IIFE 文件，页面用普通 <script> 引入。
//
// 注意：不要在这里引入 babylon-mmd 的 WASM 物理模块
// （Runtime/Optimized/...），它内含 Web Worker，Rollup 会做代码分割，
// 而 IIFE 不支持分割，构建直接失败。物理需要单独的 ESM 构建产物。
export default defineConfig({
  build: {
    outDir: "../panel/js",
    emptyOutDir: false,
    lib: {
      entry: "src/main.js",
      name: "BabylonMMDViewer",
      formats: ["iife"],
      fileName: () => "mmd.bundle.js",
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
    minify: "esbuild",
    target: "es2020",
  },
});
