// Strip provider reasoning before Markdown can turn it into a parent HTML node.
// Only leading protocol blocks are removed; code examples remain unchanged.
export function stripInlineThinking(content) {
  if (typeof content !== 'string') return ''
  let text = content
  const leadingThink = /^\s*<think(?:\s[^>]*)?>[\s\S]*?(?:<\/think\s*>|$)/i
  while (leadingThink.test(text)) text = text.replace(leadingThink, '')
  return text
}
