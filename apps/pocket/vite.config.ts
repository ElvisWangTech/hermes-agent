import react from '@vitejs/plugin-react'
import type { Plugin } from 'vite'
import { defineConfig } from 'vite'

const BACKEND = process.env.POCKET_BACKEND ?? 'http://127.0.0.1:9119'

function devSessionToken(): Plugin {
  const tokenRe = /window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"]*)"/

  return {
    name: 'pocket-dev-token',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        if (request.url === '/pocket/manifest.webmanifest') request.url = '/manifest.webmanifest'
        next()
      })
    },
    transformIndexHtml: {
      order: 'pre',
      async handler(html) {
        try {
          const page = await fetch(`${BACKEND}/`).then(response => response.text())
          const token = page.match(tokenRe)?.[1] ?? ''

          if (!token) return html

          return html.replace(
            'window.__POCKET_SESSION_TOKEN__ = ""',
            `window.__POCKET_SESSION_TOKEN__ = ${JSON.stringify(token)}`
          )
        } catch {
          return html
        }
      }
    }
  }
}

export default defineConfig({
  base: '/pocket/',
  plugins: [react(), devSessionToken()],
  server: {
    port: 5176,
    proxy: {
      '/api': { target: BACKEND, ws: true, changeOrigin: true }
    }
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    root: import.meta.dirname
  }
})
