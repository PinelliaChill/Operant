import type { Phase45 } from '@operant/sdk';

const SECRET_REF = /^[A-Z][A-Z0-9_]{1,127}$/;
const DIGEST_PINNED_IMAGE = /^(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}$/;

export interface McpFormValues {
  serverId: string;
  transport: 'stdio' | 'legacy_sse';
  stdioArgv: string;
  cwdRef: string;
  workspaceRootRef: string;
  dockerImage: string;
  endpointRef: string;
  secretRef: string;
}
function relativeCwd(value: string): boolean {
  if (!value || value === '.') return true;
  if (value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value)) return false;
  return !value.split(/[\\/]+/).includes('..');
}

export function buildMcpServerRequest(
  values: McpFormValues,
  allowedRootRefs: readonly string[],
): Phase45.McpServerBody {
  const serverId = values.serverId.trim();
  if (!serverId) throw new Error('请填写服务 ID。');

  if (values.transport === 'legacy_sse') {
    const endpointRef = values.endpointRef.trim();
    const secretRef = values.secretRef.trim();
    if (!SECRET_REF.test(endpointRef)) {
      throw new Error('Endpoint Ref 必须是环境变量名，不能填写 URL。');
    }
    if (secretRef && !SECRET_REF.test(secretRef)) {
      throw new Error('Secret Ref 必须是环境变量名，不能填写密钥。');
    }
    return {
      server_id: serverId,
      transport: 'legacy_sse',
      endpoint_ref: endpointRef,
      secret_ref: secretRef || null,
    };
  }

  let argv: unknown;
  try {
    argv = JSON.parse(values.stdioArgv);
  } catch {
    throw new Error('argv 必须是合法的 JSON 字符串数组。');
  }
  if (
    !Array.isArray(argv)
    || argv.length === 0
    || argv.length > 64
    || argv.some((part) => typeof part !== 'string' || !part || part.includes('\0'))
  ) {
    throw new Error('argv 必须包含 1–64 个非空字符串；命令和参数分开填写，不使用 Shell 字符串。');
  }
  const workspaceRootRef = values.workspaceRootRef.trim();
  if (!allowedRootRefs.includes(workspaceRootRef)) {
    throw new Error('请选择 Core 返回的工作区 Root Ref。');
  }
  const dockerImage = values.dockerImage.trim();
  if (!DIGEST_PINNED_IMAGE.test(dockerImage)) {
    throw new Error('Docker 镜像必须固定到 sha256 digest，例如 image@sha256:…。');
  }
  const cwdRef = values.cwdRef.trim() || '.';
  if (!relativeCwd(cwdRef)) {
    throw new Error('工作目录必须是 Root Ref 下的相对路径，不能使用绝对路径或 ..。');
  }
  return {
    server_id: serverId,
    transport: 'stdio',
    stdio_argv: argv as string[],
    cwd_ref: cwdRef,
    workspace_root_ref: workspaceRootRef,
    docker_image: dockerImage,
  };
}
