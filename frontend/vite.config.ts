import { fileURLToPath, URL } from 'node:url'
import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// HTTPS 证书（自签，覆盖 localhost + 局域网 IP）。IP 变了重新跑 scripts/gen-cert.bat 即可。
// 手机首次访问 https://电脑IP:5173 会提示证书不受信任，点「高级 → 继续访问」即可使用摄像头扫码。
const certDir = new URL('./certs/', import.meta.url)

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // host=true 让局域网内手机/平板也能访问（手机连同 WiFi 输 https://电脑IP:5173）
    host: true,
    // HTTPS：手机浏览器只有在安全上下文里才允许调摄像头（getUserMedia / BarcodeDetector），
    // 局域网 http 会被 Chrome/Safari 禁用，必须用 https（自签证书首次访问点「继续访问」）。
    https: {
      key: readFileSync(new URL('key.pem', certDir)),
      cert: readFileSync(new URL('cert.pem', certDir)),
    },
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
