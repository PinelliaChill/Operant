export function skillIssueCopy(issue: string): { skillName: string | null; guidance: string } {
  const skillName = issue.match(/^([\w.-]+):\s/)?.[1] ?? null;
  const lower = issue.toLowerCase();
  if (issue.includes('目录不存在或不可读取')) return { skillName: null, guidance: '目录不存在或不可读取；如需使用，请选择实际存在的目录。' };
  if (lower.includes('frontmatter structure exceeds')) return { skillName, guidance: '元数据过深或过于复杂，请检查 SKILL.md 字段。' };
  if (lower.includes('link target is outside registered roots')) return { skillName, guidance: '技能链接指向未登记的目录，请添加实际父目录。' };
  if (lower.includes('malformed or unsafe yaml')) return { skillName, guidance: '元数据格式有误，请修复 SKILL.md 开头的 YAML。' };
  return { skillName, guidance: '此目录部分技能无法读取，请查看详情。' };
}
