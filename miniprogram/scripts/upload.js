/**
 * 上传小程序代码（miniprogram-ci，无需开发者工具、无需扫码）
 *
 * 前置：把后台下载的上传密钥放到 miniprogram/private.key
 *      （后台：开发 → 开发管理 → 开发设置 → 小程序代码上传 → 生成）
 *      并在后台把本机公网 IP 加入白名单：175.153.160.165
 *
 * 用法：
 *   node scripts/upload.js            # 上传，版本号取 package.json
 *   node scripts/upload.js 1.0.1 "修复登录"   # 指定版本与备注
 */
const path = require('path')
const fs = require('fs')
const ci = require('miniprogram-ci')

const ROOT = path.resolve(__dirname, '..')
const APPID = JSON.parse(fs.readFileSync(path.join(ROOT, 'project.config.json'), 'utf8')).appid

// 上传密钥：优先 private.key，其次后台下载的 private.<appid>.key
function resolveKey() {
  const fixed = path.join(ROOT, 'private.key')
  if (fs.existsSync(fixed)) return fixed
  const hit = fs.readdirSync(ROOT).find((f) => /^private\..+\.key$/.test(f))
  return hit ? path.join(ROOT, hit) : fixed
}
const KEY = resolveKey()

async function main() {
  if (!fs.existsSync(KEY)) {
    console.error('❌ 缺少上传密钥：' + KEY)
    console.error('   请按 UPLOAD_SETUP.md 从微信后台下载密钥并放到该路径')
    process.exit(1)
  }

  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'))
  const version = process.argv[2] || pkg.version || '1.0.0'
  const desc = process.argv[3] || '自动上传'

  const project = new ci.Project({
    appid: APPID,
    type: 'miniProgram',
    projectPath: ROOT,
    privateKeyPath: KEY,
    ignores: ['node_modules/**/*', 'scripts/**/*', '*.md', '.wxsecret', 'private.key'],
  })

  console.log(`上传中 appid=${APPID} version=${version}`)
  const result = await ci.upload({
    project,
    version,
    desc,
    setting: { es6: true, minify: true, minifyWXSS: true, minifyWXML: true },
    onProgressUpdate: (p) => {
      if (p && p._status) process.stdout.write(`\r  ${p._status} ...`)
    },
  })
  console.log('\n✅ 上传完成:', JSON.stringify(result))
}

main().catch((e) => {
  console.error('\n❌ 上传失败:', e && e.message ? e.message : e)
  process.exit(1)
})
