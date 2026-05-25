import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// VITE_BACKEND_URL: set this in Cloudflare Pages env to point to your Oracle VM
// e.g. https://your-vm-domain.com
// Leave unset for local dev (uses proxy)
const backendUrl = process.env.VITE_BACKEND_URL || ''

export default defineConfig({
  plugins: [react()],
  define: {
    // Expose backend URL to the app bundle
    __BACKEND_URL__: JSON.stringify(backendUrl),
  },
  server: {
    proxy: {
      '/ws': { target: 'ws://localhost:8000', ws: true },
      '/api': { target: 'http://localhost:8000' },
    },
  },
  build: { outDir: 'dist' },
})