/** Resolve only a local WebSocket endpoint; its one-use token never enters the URL. */
export function resolveTerminalStreamUrl(origin: string, terminalId: string): string {
  if (!terminalId) throw new Error('Core 未返回有效终端 ID，输入已停止。');
  const url = new URL(origin);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
    throw new Error('交互终端仅允许本机连接。');
  }
  const stream = new URL(`/v1/workbench/terminals/${encodeURIComponent(terminalId)}/stream`, url);
  stream.protocol = stream.protocol === 'https:' ? 'wss:' : 'ws:';
  return stream.toString();
}

export function terminalStreamProtocols(token: string | null | undefined): [string, string] {
  if (!token || !/^[A-Za-z0-9_-]+$/.test(token)) throw new Error('Core 未返回有效终端连接凭据，输入已停止。');
  return ['operant.terminal.v1', `operant.token.${token}`];
}
