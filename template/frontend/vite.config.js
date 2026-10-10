import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { pageDataPlugin } from './page-data-plugin.js'

const backendPort = Number(process.env.ARC_WEB_PORT || 3000)

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    pageDataPlugin(backendPort),
    react(),
    tailwindcss(),
  ],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './test/setup.ts',
    include: ['tests/**/*.{test,spec}.{js,jsx,ts,tsx}'],
  },
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${backendPort}`
    }
  }
})
