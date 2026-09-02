import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base '/dashboard/' -- Flask serves the built index.html at GET /dashboard
// and the hashed asset files this produces at /dashboard/assets/*.
export default defineConfig({
  plugins: [react()],
  base: '/dashboard/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:5000',
    },
  },
});
