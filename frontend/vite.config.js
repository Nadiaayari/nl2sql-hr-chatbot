import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/register': 'http://localhost:8000',
      '/login': 'http://localhost:8000',
      '/query': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/cache': 'http://localhost:8000',
      '/me': 'http://localhost:8000',
      '/history': 'http://localhost:8000',
      '/admin': 'http://localhost:8000',
    }
  }
})