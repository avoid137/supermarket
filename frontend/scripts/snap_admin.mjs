// 截图脚本：访问 /admin，等数据加载完成，截图保存
import { chromium } from 'playwright'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 1600 } })
  const page = await context.newPage()

  await page.goto('http://127.0.0.1:5173/admin', { waitUntil: 'networkidle', timeout: 30000 })
  // 等数据：等待 KPI 卡出现具体数字
  await page.waitForFunction(
    () => {
      const nums = document.querySelectorAll('.kpi-num')
      return nums.length === 4 && Array.from(nums).every((n) => n.textContent !== '–' && n.textContent?.length)
    },
    { timeout: 20000 },
  )
  await page.waitForTimeout(500)  // 让 SVG 渲染

  // 输出到仓库根目录（可用命令行参数覆盖），不写死本机绝对路径
  const out = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.resolve(__dirname, '..', '..', 'admin_screenshot.png')
  await page.screenshot({ path: out, fullPage: true })
  console.log(`截图已保存：${out}`)

  await browser.close()
}

main().catch((e) => {
  console.error('FAIL:', e.message)
  process.exit(1)
})
