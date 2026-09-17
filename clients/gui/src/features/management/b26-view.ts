import type { B26Command, B26State, SourceRef, MemoryVersionRef } from '../../../../../sdk/typescript-client/b2_6.generated';
import type { GovernanceState } from '../../../../../sdk/typescript-client/b2_5.generated';
import type { ManagementState } from '../../../../../sdk/typescript-client/b2_3.generated';
import type { B26Action, B26Field, B26Section } from './b26-presentation-types';

export interface ExperienceView {
  sections: B26Section[];
  commands: Map<string, (values: Record<string, string>) => B26Command>;
}
const fact = (label: string, value: unknown) => ({ label, value: typeof value === 'string' ? value : JSON.stringify(value) ?? '—' });
const stateText = (value: string | undefined) => ({
  active: '有效', published: '已发布', draft: '草稿', pending: '待验证', passed: '已通过',
  blocked: '不可用', revoked: '已撤销', disabled: '已停用', expired: '已过期',
  validated: '已验证', untrusted_draft: '未验证草稿', core_published: '已验证发布',
}[value ?? ''] ?? value ?? '未知');
const field = (key: string, label: string, kind: B26Field['kind'] = 'text'): B26Field => ({ key, label, kind, required: true });
function required(values: Record<string, string>, key: string): string {
  const value = values[key]?.trim();
  if (!value) throw new Error('请填写所有必要字段。');
  return value;
}
function number(values: Record<string, string>, key: string): number {
  const value = Number(required(values, key));
  if (!Number.isSafeInteger(value) || value < 0) throw new Error('请输入有效整数。');
  return value;
}
function key(ref: MemoryVersionRef): string { return `${ref.dataset_id}:${ref.record_id}@${ref.version}:${ref.content_digest}`; }

/** Bind every visible action to the exact projection from which it was rendered. */
export function buildExperienceView(state: B26State, governance: GovernanceState, management: ManagementState, generation: number): ExperienceView {
  const projectId = state.project_id;
  const commands = new Map<string, (values: Record<string, string>) => B26Command>();
  const sections: B26Section[] = [
    { id: 'skills', title: '经验技能', description: '将有来源的经验整理为步骤，经审阅和验证后发布。技能资料不会增加工具权限。', emptyMessage: '还没有经验技能。先创建步骤候选，在知识治理中确认后再生成技能草稿。', cards: [], actions: [] },
    { id: 'writers', title: 'Writer 知识晋级', description: '分支经验保持隔离；Core 核对合并结果和目标 tree 的验证证据后，才能晋级到项目。', emptyMessage: '暂无待晋级的 Writer 经验。', cards: [], actions: [] },
    { id: 'sharing', title: '授权共享', description: '共享必须明确对象、范围和期限。撤销阻止后续使用，已发送内容无法收回。', emptyMessage: '没有显式共享授权。路径或仓库地址相同不会自动共享。', cards: [], actions: [] },
    { id: 'remote', title: 'Remote 最小记忆包', description: '只向已授权 Target 提供当前任务所需内容；用途、期限与本地来源复核持续生效。', emptyMessage: '暂无远程记忆包。', cards: [], actions: [] },
  ];
  function add(section: B26Section, identity: string, action: Omit<B26Action, 'id'>, build: (v: Record<string, string>) => B26Command) {
    const id = `${projectId}:${generation}:${identity}`;
    section.actions.push({ ...action, id });
    commands.set(id, build);
  }
  const usable = governance.records.filter(r => r.currently_usable);
  const records = new Map(usable.map(r => [key(r.version.ref), r]));
  const recordOptions = usable.map(r => ({ value: key(r.version.ref), label: `${r.version.content.slice(0, 60)} · v${r.version.ref.version}` }));
  const recordField: B26Field = { ...field('source', '来源记录', 'select'), options: recordOptions };
  const shareable = [...usable.map(r => r.version), ...(state.sharing.personal_preferences ?? [])];
  const shareRecordField: B26Field = { ...recordField, label: '要授权的精确记录', options: shareable.map(v => ({ value: key(v.ref), label: `${v.scope.kind === 'personal' ? '个人偏好 · ' : ''}${v.content.slice(0, 60)} · v${v.ref.version}` })) };
  const selectedRecord = (v: Record<string, string>) => {
    const item = records.get(required(v, 'source'));
    if (!item) throw new Error('来源不在当前已核对列表中，请刷新。');
    return item;
  };
  add(sections[0], 'procedure_propose', {
    title: '整理经验步骤', description: '以当前发布记录为来源，提出可复用步骤；提交后仍需在知识治理中审阅。', confirmLabel: '提交步骤候选',
    fields: [recordField, field('content', '可复用步骤', 'textarea')], disabled: !usable.length,
    disabledReason: '需要至少一条当前可用的正式记录。',
  }, v => {
    const version = selectedRecord(v).version;
    const source: SourceRef = { source_type: 'memory_version', source_id: version.ref.record_id, revision: version.ref.version,
      content_digest: version.ref.content_digest, scope: version.scope, permission_epoch: state.remote.permission_epoch ?? 0, availability: 'available' };
    return { action: 'procedure_propose', project_id: projectId, content: required(v, 'content'), sources: [source] };
  });
  const procedures = usable.filter(r => r.version.content_type === 'procedure');
  const procedureOptions = procedures.map(r => ({ value: key(r.version.ref), label: `${r.version.content.slice(0, 60)} · v${r.version.ref.version}` }));
  const procedureField = { ...recordField, label: '已确认的步骤', options: procedureOptions };
  add(sections[0], 'skill_draft', { title: '生成技能草稿', description: '冻结步骤版本、来源与资源 hash，生成待验证的技能。', confirmLabel: '生成草稿',
    fields: [procedureField, field('name', '技能标识（英文小写、数字或连字符）'), field('description', '用途说明', 'textarea')], disabled: !procedures.length,
    disabledReason: '请先审阅并确认步骤候选。',
  }, v => ({ action: 'skill_draft', project_id: projectId, procedure_ref: selectedRecord(v).version.ref, name: required(v, 'name'), description: required(v, 'description') }));
  for (const skill of state.skills.skills ?? []) {
    const version = skill.version;
    const exact = { project_id: projectId, skill_id: skill.skill_id, skill_version: version.version, expected_head_revision: skill.head.head_revision, permission_epoch: skill.head.permission_epoch };
    sections[0].cards.push({ id: skill.skill_id, title: version.name, status: `展示 v${version.version} · 已发布 ${skill.head.published_version ?? '无'} · ${stateText(skill.head.state)}`, description: version.description,
      facts: [fact('发布版本', skill.head.published_version), fact('来源步骤', key(version.procedure_ref)), fact('来源状态', stateText(skill.dependency_state)), fact('验证', stateText(skill.validation?.status ?? 'pending')), fact('信任', stateText(skill.trust_status ?? version.trust_status)), fact('资源 hash', version.artifact.content_hash), fact('角色限制', version.role_ids), fact('可回退版本', skill.rollback_versions)],
      warning: skill.dependency_state !== 'active' ? '来源已不可用，后续使用会被阻止。' : undefined });
    for (const [action, title] of [['skill_validate', '验证技能'], ['skill_publish', '发布技能'], ['skill_disable', '停用技能']] as const) {
      const unavailable = action === 'skill_disable' ? skill.head.published_version == null || skill.head.state !== 'published' : skill.dependency_state !== 'active';
      add(sections[0], `${action}:${skill.skill_id}:${skill.head.head_revision}:${version.version}`, { title: `${title} · ${version.name}`, description: `核对技能 v${version.version}、发布头修订 ${skill.head.head_revision} 后操作。`, confirmLabel: title, fields: [], dangerous: action === 'skill_disable', disabled: unavailable, disabledReason: action === 'skill_disable' ? '当前没有可停用的已发布版本。' : '来源当前不可用，请先核对来源。' }, () => {
        if (unavailable) throw new Error('当前技能状态不允许此操作，请刷新核对。');
        return { ...exact, action, skill_version: action === 'skill_disable' ? skill.head.published_version! : version.version };
      });
    }
    add(sections[0], `skill_rollback:${skill.skill_id}:${skill.head.head_revision}`, { title: `回退 · ${version.name}`, description: '旧版本仍需通过当前来源与权限检查。', confirmLabel: '回退到所选版本', dangerous: true,
      fields: [{ ...field('version', '回退版本', 'select'), options: (skill.rollback_versions ?? []).map(n => ({ value: String(n), label: `v${n}` })) }], disabled: skill.head.published_version == null || !skill.rollback_versions?.length,
      disabledReason: '需要已有发布版本和通过验证的回退目标。',
    }, v => {
      if (skill.head.published_version == null) throw new Error('技能尚未发布，不能回退。');
      const target = number(v, 'version');
      if (!skill.rollback_versions?.includes(target)) throw new Error('目标版本不在当前可回退列表中。');
      return { ...exact, skill_version: skill.head.published_version, action: 'skill_rollback', rollback_to_version: target };
    });
    add(sections[0], `skill_new_version:${skill.skill_id}:${skill.head.head_revision}`, { title: `更新草稿 · ${version.name}`, description: '从已确认的最新步骤生成新版本，旧发布版保持可追溯。', confirmLabel: '创建新版本', fields: [procedureField], disabled: !procedures.length }, v => ({ action: 'skill_draft', ...exact, skill_version: skill.head.published_version, procedure_ref: selectedRecord(v).version.ref, name: version.name, description: version.description }));
  }
  add(sections[1], 'worktree_register', { title: '登记项目工作区', description: '选择已由 Core 初始化的工作区；Core 读取当前 Git 分支、commit 与 tree，登记不自动授予共享权。', confirmLabel: '登记工作区',
    fields: [{ ...field('workspace', '已登记 Workspace', 'select'), options: management.projects.filter(p => !p.archived).map(p => ({ value: p.workspace_id, label: `${p.name} · ${p.workspace_id}` })) }],
  }, v => ({ action: 'worktree_register', project_id: projectId, workspace_id: required(v, 'workspace') }));
  const projectOptions = management.projects.filter(p => !p.archived).map(p => ({ value: p.project_id, label: p.name }));
  add(sections[2], 'personal_preference_create', { title: '保存个人偏好', description: '明确选择来源并确认个人偏好。偏好保存在当前用户范围，只有另行授权后才用于其他项目。', confirmLabel: '确认保存个人偏好', fields: [recordField, field('content', '偏好内容', 'textarea')], disabled: !usable.length, disabledReason: '需要当前可用的来源记录。' }, v => {
    const source = selectedRecord(v).version;
    return { action: 'personal_preference_create', project_id: projectId, content: required(v, 'content'), source_refs: [{ source_type: 'memory_version', source_id: source.ref.record_id, revision: source.ref.version, content_digest: source.ref.content_digest, scope: source.scope, permission_epoch: state.remote.permission_epoch ?? 0, availability: 'available' }] };
  });
  for (const preference of state.sharing.personal_preferences ?? []) sections[2].cards.push({
    id: key(preference.ref), title: '个人偏好', status: '已发布', description: preference.content,
    facts: [fact('范围', '仅当前用户；跨项目使用需显式授权'), fact('精确版本', key(preference.ref)), fact('所有者', preference.owner.principal_id), fact('来源数', preference.sources.length)],
  });
  add(sections[2], 'grant_create', { title: '授权精确知识', description: '只授权选中的记录版本；收件角色或模型、用途、目标项目和期限都必须明确。原来源的限制继续生效。', confirmLabel: '创建授权',
    fields: [shareRecordField, { ...field('target_project', '目标项目', 'select'), options: projectOptions }, field('subject', '授权对象 ID（角色、模型配置或移交目标安装）'),
      { ...field('purpose', '用途', 'select'), options: [{ value: 'recall', label: '后续任务召回' }, { value: 'transfer', label: '数据集移交' }] },
      { ...field('hours', '授权有效小时数', 'number'), value: '24', min: 1, max: 168 }], disabled: !shareable.length,
  }, v => {
    const source = shareable.find(item => key(item.ref) === required(v, 'source'));
    if (!source) throw new Error('记录不在当前已核对列表中，请刷新。');
    const target = management.projects.find(p => p.project_id === required(v, 'target_project') && !p.archived);
    if (!target) throw new Error('目标项目已不在当前可用列表中。');
    const purpose = required(v, 'purpose');
    if (purpose !== 'recall' && purpose !== 'transfer') throw new Error('用途不在允许列表中。');
    const hours = number(v, 'hours');
    if (hours < 1 || hours > 168) throw new Error('授权期限应为 1～168 小时。');
    return { action: 'grant_create', project_id: projectId, grant: {
      project_id: projectId, source_dataset_id: source.ref.dataset_id, source_scope: source.scope,
      target_scope: { kind: 'workspace', project_id: target.project_id, workspace_id: target.workspace_id },
      subject_id: required(v, 'subject'), grantor_id: source.owner.principal_id, purpose,
      memory_refs: [source.ref], permission_epoch: state.remote.permission_epoch ?? 0,
      expires_at: new Date(Date.now() + hours * 3600000).toISOString(),
    } };
  });
  const sourceDataset = state.datasets?.find(d => d.dataset_id === state.remote.dataset_id);
  const transferGrants = (state.sharing.grants ?? []).filter(g => g.state === 'active' && g.purpose === 'transfer');
  add(sections[2], 'dataset_transfer_begin', { title: '移交数据集', description: '保持数据集身份，将实际所有权移交给所选安装；Core 校验授权、消费者及目标安装状态。', confirmLabel: '准备移交',
    fields: [{ ...field('destination', '目标插件安装', 'select'), options: management.installations.filter(i => i.dataset_id !== sourceDataset?.dataset_id).map(i => ({ value: i.installation_id, label: `${i.plugin_id} · ${i.installation_id}` })) },
      { ...field('grant', '已确认的移交授权', 'select'), options: transferGrants.filter(g => g.grant_id).map(g => ({ value: g.grant_id!, label: `${g.subject_id} · ${g.expires_at}` })) }],
    disabled: !sourceDataset || !transferGrants.length, disabledReason: '需要当前数据集和有效的移交授权。',
  }, v => {
    if (!sourceDataset) throw new Error('当前数据集不可用。');
    const grant = transferGrants.find(g => g.grant_id === required(v, 'grant'));
    const installation = management.installations.find(i => i.installation_id === required(v, 'destination'));
    if (!grant?.grant_id || !installation) throw new Error('移交对象或授权已经变化。');
    return { action: 'dataset_transfer_begin', project_id: projectId, installation_id: sourceDataset.installation_id,
      transfer: { dataset_id: sourceDataset.dataset_id, destination_dataset_id: sourceDataset.dataset_id,
        expected_revision: sourceDataset.revision, from_namespace: `dataset:${sourceDataset.dataset_id}`, to_namespace: `dataset:${sourceDataset.dataset_id}`,
        destination_installation_id: installation.installation_id, authorization_grant_id: grant.grant_id,
        mode: 'transfer', idempotency_key: crypto.randomUUID() } };
  });
  for (const wt of state.sharing.worktrees ?? []) {
    sections[1].cards.push({ id: wt.registration_id ?? wt.worktree_id, title: wt.branch_ref, status: wt.state ?? 'active', facts: [fact('Workspace', wt.workspace_id), fact('注册 ID', wt.registration_id), fact('Commit', wt.commit_ref), fact('Tree', wt.tree_digest)] });
    if (wt.registration_id) add(sections[1], `worktree_revoke:${wt.registration_id}:${wt.association_revision}`, { title: `撤销注册 · ${wt.branch_ref}`, description: '停止此 worktree 的授权关联，既有来源仍留可追溯记录。', confirmLabel: '撤销注册', fields: [field('reason', '原因')], dangerous: true }, v => ({ action: 'worktree_revoke', project_id: projectId, registration_id: wt.registration_id, expected_revision: wt.association_revision ?? 0, reason: required(v, 'reason') }));
  }
  add(sections[1], 'writer_memory_propose', { title: '记录 Writer 分支经验', description: '经验绑定到明确的 Writer 工作区和 Run；提交后保留为隔离候选，不会进入项目召回。', confirmLabel: '记录待晋级经验',
    fields: [recordField, { ...field('worktree', '已登记 worktree', 'select'), options: (state.sharing.worktrees ?? []).filter(w => w.state === 'active').map(w => ({ value: w.worktree_id, label: `${w.branch_ref} · ${w.worktree_id}` })) }, field('writer', 'Writer Workspace ID'), field('run', 'Graph Run ID'), field('content', '分支实验结论', 'textarea')],
    disabled: !usable.length || !state.sharing.worktrees?.length,
  }, v => {
    const version = selectedRecord(v).version;
    return { action: 'writer_memory_propose', project_id: projectId, worktree_id: required(v, 'worktree'), writer_workspace_id: required(v, 'writer'), run_id: required(v, 'run'), graph_run_id: required(v, 'run'), content: required(v, 'content'),
      source_refs: [{ source_type: 'memory_version', source_id: version.ref.record_id, revision: version.ref.version, content_digest: version.ref.content_digest, scope: version.scope, permission_epoch: state.remote.permission_epoch ?? 0, availability: 'available' }] };
  });
  for (const candidate of state.sharing.writer_candidates ?? []) sections[1].cards.push({
    id: `candidate:${candidate.evidence_id}`, title: '待晋级的分支经验', status: candidate.state,
    description: candidate.memory.content, facts: [fact('版本', key(candidate.memory.ref)), fact('Run', candidate.run_id), fact('分支', candidate.branch_ref), fact('角色限制', candidate.memory.role_ids), fact('敏感级别', candidate.memory.sensitivity)],
    warning: '尚未完成晋级前，此内容不属于项目正式知识。',
  });
  for (const writer of state.sharing.writer_evidence ?? []) {
    sections[1].cards.push({ id: writer.evidence_id ?? writer.memory_ref.record_id, title: writer.branch_ref, status: writer.state ?? 'candidate', facts: [fact('来源版本', key(writer.memory_ref)), fact('Run', writer.run_id), fact('Merge', writer.merge_run_id), fact('目标 tree', writer.target_tree_digest), fact('验证', writer.verification_status)], warning: writer.reason ?? undefined });
    const exact = { project_id: projectId, evidence_id: writer.evidence_id, expected_revision: writer.revision ?? 0 };
    add(sections[1], `writer_verify:${writer.evidence_id}:${writer.revision}`, { title: `核对合并目标 · ${writer.branch_ref}`, description: 'Core 实际核对合并结果、目标提交和干净工作区，并保存核对工件。这不代表业务测试通过，也不提高原结论的可信度。', confirmLabel: '核对合并目标', fields: [field('merge', 'Merge Run ID')] }, v => ({ ...exact, action: 'writer_memory_verify', merge_run_id: required(v, 'merge') }));
    add(sections[1], `writer_promote:${writer.evidence_id}:${writer.revision}`, { title: `晋级到项目 · ${writer.branch_ref}`, description: '发布前再次核对目标 tree、来源和最严格可见性。', confirmLabel: '晋级知识', fields: [], disabled: writer.state !== 'eligible', disabledReason: '需要已合并且通过目标 tree 验证的证据。' }, () => ({ ...exact, action: 'writer_memory_promote' }));
  }
  for (const grant of state.sharing.grants ?? []) {
    sections[2].cards.push({ id: grant.grant_id ?? grant.subject_id, title: grant.subject_id, status: grant.state ?? 'active', facts: [fact('来源范围', grant.source_scope), fact('授权范围', grant.target_scope), fact('用途', grant.purpose), fact('期限', grant.expires_at), fact('精确记录数', grant.memory_refs.length)] });
    add(sections[2], `grant_revoke:${grant.grant_id}:${grant.revision}`, { title: `撤销授权 · ${grant.subject_id}`, description: '阻止后续使用；已发送或导出的副本无法收回。', confirmLabel: '撤销授权', fields: [field('reason', '撤销原因')], dangerous: true }, v => ({ action: 'grant_revoke', project_id: projectId, grant_id: grant.grant_id, expected_revision: grant.revision ?? 0, reason: required(v, 'reason') }));
  }
  for (const transfer of state.sharing.transfers ?? []) {
    sections[2].cards.push({ id: transfer.transfer_id ?? transfer.request.dataset_id, title: '数据集移交', status: transfer.state ?? 'pending', facts: [fact('源数据集', transfer.request.dataset_id), fact('目标数据集', transfer.request.destination_dataset_id), fact('目标安装', transfer.request.destination_installation_id), fact('验证证据', transfer.evidence_refs)], warning: transfer.reason ?? undefined });
    for (const [action, title] of [['dataset_transfer_validate', '验证移交'], ['dataset_transfer_commit', '完成移交'], ['dataset_transfer_revoke', '撤销移交']] as const) add(sections[2], `${action}:${transfer.transfer_id}:${transfer.revision}`, { title, description: 'Core 检查当前所有权、授权与共享消费者，原插件卸载不能删除新所有者数据。', confirmLabel: title, fields: [], dangerous: action === 'dataset_transfer_revoke' }, () => ({ action, project_id: projectId, transfer_id: transfer.transfer_id, expected_revision: transfer.revision ?? 0 }));
  }
  for (const pack of state.remote.packs ?? []) {
    sections[3].cards.push({ id: pack.package_id, title: pack.purpose, status: pack.status, facts: [fact('Target', pack.target_id), fact('期限', pack.expires_at), fact('条目数', pack.entry_count), fact('包 hash', pack.package_digest)] });
    add(sections[3], `remote_revoke:${pack.package_id}:${pack.package_digest}`, { title: `撤销包 · ${pack.purpose}`, description: '阻止此包后续使用和上传候选；已经交付的正文无法收回。', confirmLabel: '撤销包', fields: [], dangerous: true }, () => ({ action: 'remote_pack_revoke', project_id: projectId, target_id: pack.target_id, purpose: pack.purpose, package_id: pack.package_id, package_digest: pack.package_digest }));
  }
  add(sections[3], 'remote_pack_create', { title: '准备最小记忆包', description: '选择明确记录和本地 Session，Core 按该角色与模型、Target 授权及期限裁决。', confirmLabel: '准备受限包',
    fields: [recordField, field('target', '已注册 Target ID'), field('session', '当前任务 Session ID'), { ...field('purpose', '用途标识'), value: 'remote_execution' }, { ...field('ttl', '有效秒数（1～300）', 'number'), value: '120', min: 1, max: 300 }], disabled: !usable.length,
  }, v => ({ action: 'remote_pack_create', project_id: projectId, target_id: required(v, 'target'), session_id: required(v, 'session'), purpose: required(v, 'purpose'), ttl_seconds: number(v, 'ttl'), record_ids: [selectedRecord(v).version.ref.record_id], limit: 1, max_tokens: 2000, max_bytes: 32000 }));
  return { sections, commands };
}
