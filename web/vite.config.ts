import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API is served by the Python process. In dev, Vite proxies to it so the
// browser sees a single origin and EventSource works without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5188,
    proxy: {
      '/api': {
        target: process.env.ACM_API ?? 'http://127.0.0.1:8808',
        changeOrigin: true,
        // SSE must not be buffered by the proxy.
        configure: (proxy) => {
          proxy.on('proxyRes', (res) => {
            res.headers['cache-control'] = 'no-cache, no-transform'
          })
        },
      },
    },
  },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 900 },
})
