import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { B25ClientLike, B2ModelClientLike } from './b25-state';
import { useB25Governance } from './b25-state';
import { B25Presentation } from './B25Presentation';
import { SearchSelect } from '../../components/SearchSelect';

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
        </div>
      </div>

      <div className="b2-memory-card b25-governance-scope">
        <SearchSelect
          label="项目范围"
          value={projectId}
          onChange={setProjectId}
          disabled={activeProjects.length === 0}
          placeholder={activeProjects.length ? '请选择项目' : '暂无可用项目'}
          options={activeProjects.map((project) => ({ value: project.project_id, label: project.name }))}
        />
        {connectionStatus !== 'connected' && (
          <p className="b2-memory-warning" role="status">
            连接已断开。已读取的内容仍可查看；恢复连接后请刷新。
          </p>
        )}
        {!client && (
          <p className="b2-memory-warning" role="alert">
            高级知识整理暂不可用。
          </p>
        )}
        {client && !controller.supported && (
          <p className="b2-memory-warning" role="alert">
            当前服务版本不支持高级知识整理。请更新后重试。
          </p>
        )}
      </div>

      <B25Presentation {...controller} />
    </section>
  );
};
