import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { B25ClientLike, B2ModelClientLike } from './b25-state';
import { useB25Governance } from './b25-state';
import { B25Presentation } from './B25Presentation';

export interface B25GovernanceProject {
  project_id: string;
  name: string;
  archived?: boolean;
}

export interface B25GovernancePanelProps {
  client: B25ClientLike | null;
  modelClient: B2ModelClientLike | null;
  projects: B25GovernanceProject[];
  connectionStatus: string;
  initialProjectId?: string;
  onMutation?: () => void;
}

function firstActiveProject(projects: B25GovernanceProject[], preferred?: string): string {
  if (preferred && projects.some((project) => project.project_id === preferred && !project.archived)) {
    return preferred;
  }
  return projects.find((project) => !project.archived)?.project_id ?? '';
}

/**
 * B2-5 business parent.  Presentation receives only a server projection and
 * callbacks; all identity, pagination, selection and command safety lives in
 * useB25Governance.
 */
export const B25GovernancePanel: React.FC<B25GovernancePanelProps> = ({
  client,
  modelClient,
  projects,
  connectionStatus,
  initialProjectId,
  onMutation,
}) => {
  const activeProjects = useMemo(() => projects.filter((project) => !project.archived), [projects]);
  const [projectId, setProjectId] = useState(() => firstActiveProject(projects, initialProjectId));
  const previousInitialProjectId = useRef(initialProjectId);

  useEffect(() => {
    const initialChanged = initialProjectId !== previousInitialProjectId.current;
    previousInitialProjectId.current = initialProjectId;
    setProjectId((current) => {
      if (initialChanged && initialProjectId && activeProjects.some((project) => project.project_id === initialProjectId)) {
        return initialProjectId;
      }
      if (current && activeProjects.some((project) => project.project_id === current)) return current;
      return firstActiveProject(activeProjects, initialProjectId);
    });
  }, [activeProjects, initialProjectId]);

  const controller = useB25Governance({
    client,
    modelClient,
    projectId: projectId || null,
    connected: connectionStatus === 'connected',
    onMutation,
  });

  return (
    <section className="b2-memory b25-governance-panel" data-panel="b25-governance" aria-labelledby="b25-governance-title">
      <div className="b2-memory-card-header">
        <div>
          <h2 id="b25-governance-title">高级知识治理</h2>
          <p>当前约定、历史事实、候选冲突和后台整理都以 B2-5 服务端 Projection 为准。</p>
        </div>
        <span className="b2-memory-status">B2-5 / MP-4</span>
      </div>

      <div className="b2-memory-card b25-governance-scope">
        <label htmlFor="b25-governance-project">项目范围</label>
        <select
          id="b25-governance-project"
          className="select"
          value={projectId}
          onChange={(event) => setProjectId(event.target.value)}
          disabled={activeProjects.length === 0}
        >
          <option value="">{activeProjects.length ? '请选择项目' : '服务端尚未返回可用项目'}</option>
          {activeProjects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name} · {project.project_id}
            </option>
          ))}
        </select>
        {connectionStatus !== 'connected' && (
          <p className="b2-memory-warning" role="status">
            Core 连接已断开；保留已读取的治理内容，只读操作可继续查看，写操作已禁用。恢复连接后请刷新。
          </p>
        )}
        {!client && (
          <p className="b2-memory-warning" role="alert">
            B2-5 高级治理接口尚未接入；现有基础管理仍可继续使用。
          </p>
        )}
        {client && !controller.supported && (
          <p className="b2-memory-warning" role="alert">
            当前 Core 不支持 B2-5 高级治理接口；现有基础管理仍可继续使用，未提交任何 B2-5 命令。
          </p>
        )}
      </div>

      <B25Presentation {...controller} />
    </section>
  );
};
