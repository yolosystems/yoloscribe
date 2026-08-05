import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Absolute asset paths: the SPA is served from the bucket root and CloudFront
  // returns index.html as the fallback for every route, so assets must load from
  // /assets/ regardless of the route's path depth. Relative ('./') breaks any
  // route deeper than one segment (e.g. /oauth/consent → /oauth/assets/ 404).
  base: '/',
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
