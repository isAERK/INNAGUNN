import { defineConfig } from "vite";

export default defineConfig({
  // Windows can throw EBUSY if Vite tries to watch Cargo/Rust build artifacts
  // while tauri dev is compiling. Those folders are not frontend source.
  server: {
    watch: {
      ignored: [
        "**/src-tauri/target/**",
        "**/src-tauri/binaries/**",
        "**/dist/**",
        "**/build/**",
      ],
    },
  },
});
