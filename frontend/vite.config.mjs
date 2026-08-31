import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  // Electron loads the production page through file://, so bundled assets
  // must be resolved relative to build/index.html instead of the disk root.
  base: './',
  plugins: [react()],
  build: {
    outDir: 'build',
    chunkSizeWarningLimit: 1000
  },
  server: {
    host: '127.0.0.1',
    port: 3000
  }
});
