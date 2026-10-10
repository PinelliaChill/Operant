/** Origin-scoped storage for non-exportable WebCrypto keys and public pairing state. */
export interface PairingIdentity {
  deviceId: string;
  baseUrl: string;
  coreEpochId: string;
  coreSigningPublicKey: string;
  coreExchangePublicKey: string;
  signingPrivateKey: CryptoKey;
  exchangePrivateKey: CryptoKey;
  signingPublicKey: string;
  exchangePublicKey: string;
  expiresAt: number;
  paired: boolean;
}

const databaseName = 'operant-caller-pairing-v1';
const storeName = 'identities';
const pendingStoreName = 'pendingWrites';
const deviceIdPattern = /^caller_[0-9a-f]{32}$/;
const coreIdPattern = /^core_[0-9a-f]{32}$/;
const publicKeyPattern = /^[A-Za-z0-9_-]{87}$/;

function validIdentity(value: unknown): value is PairingIdentity {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<PairingIdentity>;
  let validOrigin = false;
  try {
    const url = new URL(item.baseUrl ?? '');
    validOrigin = url.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(url.hostname)
      && /^\d+$/.test(url.port) && url.origin === item.baseUrl;
  } catch { /* invalid stored address */ }
  return validOrigin && typeof item.deviceId === 'string' && deviceIdPattern.test(item.deviceId)
    && typeof item.coreEpochId === 'string' && coreIdPattern.test(item.coreEpochId)
    && typeof item.coreSigningPublicKey === 'string' && publicKeyPattern.test(item.coreSigningPublicKey)
    && typeof item.coreExchangePublicKey === 'string' && publicKeyPattern.test(item.coreExchangePublicKey)
    && typeof item.signingPublicKey === 'string' && publicKeyPattern.test(item.signingPublicKey)
    && typeof item.exchangePublicKey === 'string' && publicKeyPattern.test(item.exchangePublicKey)
    && item.signingPrivateKey?.type === 'private' && item.signingPrivateKey.extractable === false
    && item.exchangePrivateKey?.type === 'private' && item.exchangePrivateKey.extractable === false
    && typeof item.expiresAt === 'number' && Number.isSafeInteger(item.expiresAt)
    && typeof item.paired === 'boolean';
}

export interface PendingPairedWrite {
  deviceId: string;
  coreEpochId: string;
  requestId: string;
  operation: 'add' | 'remove';
  payloadHash: string;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise<IDBDatabase>((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new Error('此浏览器无法保存配对密钥，请使用支持 IndexedDB 的浏览器。'));
      return;
    }
    const request = indexedDB.open(databaseName, 2);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(storeName)) {
        request.result.createObjectStore(storeName, { keyPath: 'deviceId' });
      }
      if (!request.result.objectStoreNames.contains(pendingStoreName)) {
        request.result.createObjectStore(pendingStoreName, { keyPath: 'deviceId' });
      }
    };
    request.onerror = () => reject(new Error('无法打开本机配对存储。'));
    request.onsuccess = () => resolve(request.result);
  });
}

function transaction<T>(database: IDBDatabase, storeNameToUse: string, mode: IDBTransactionMode, run: (store: IDBObjectStore, setResult: (value: T) => void, reject: (error: Error) => void) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const work = database.transaction(storeNameToUse, mode);
    let result: T;
    work.oncomplete = () => resolve(result);
    work.onerror = () => reject(new Error('无法保存本机配对状态。'));
    work.onabort = () => reject(new Error('本机配对状态保存中断。'));
    run(work.objectStore(storeNameToUse), (value) => { result = value; }, reject);
  }).finally(() => database.close());
}

export async function savePairingIdentity(identity: PairingIdentity): Promise<void> {
  if (identity.signingPrivateKey.extractable || identity.exchangePrivateKey.extractable) {
    throw new Error('配对私钥必须不可导出。');
  }
  const database = await openDatabase();
  await transaction<void>(database, storeName, 'readwrite', (store, setResult, reject) => {
    const request = store.put(identity);
    request.onerror = () => reject(new Error('无法保存本机配对密钥。'));
    request.onsuccess = () => setResult();
  });
}

export async function listPairingIdentities(): Promise<PairingIdentity[]> {
  const database = await openDatabase();
  return transaction<PairingIdentity[]>(database, storeName, 'readonly', (store, setResult, reject) => {
    const request = store.getAll();
    request.onerror = () => reject(new Error('无法读取本机配对密钥。'));
    request.onsuccess = () => {
      const items: unknown = request.result;
      if (!Array.isArray(items) || !items.every(validIdentity)) {
        reject(new Error('保存的配对状态无效。请检查浏览器存储。'));
        return;
      }
      setResult(items);
    };
  });
}

export async function deletePairingIdentity(deviceId: string): Promise<void> {
  const database = await openDatabase();
  await transaction<void>(database, storeName, 'readwrite', (store, setResult, reject) => {
    const request = store.delete(deviceId);
    request.onerror = () => reject(new Error('无法移除本机配对密钥。'));
    request.onsuccess = () => setResult();
  });
}

export async function reservePendingPairedWrite(value: PendingPairedWrite): Promise<void> {
  const database = await openDatabase();
  await transaction<void>(database, pendingStoreName, 'readwrite', (store, setResult, reject) => {
    const current = store.get(value.deviceId);
    current.onerror = () => reject(new Error('无法检查待核对的技能来源请求。'));
    current.onsuccess = () => {
      if (current.result) { reject(new Error('上次修改仍待核对，请先核对原请求。')); return; }
      const saved = store.add(value);
      saved.onerror = () => reject(new Error('无法保存待核对的技能来源请求。'));
      saved.onsuccess = () => setResult();
    };
  });
}

export async function readPendingPairedWrite(deviceId: string): Promise<PendingPairedWrite | null> {
  const database = await openDatabase();
  return transaction<PendingPairedWrite | null>(database, pendingStoreName, 'readonly', (store, setResult, reject) => {
    const request = store.get(deviceId);
    request.onerror = () => reject(new Error('无法读取待核对的技能来源请求。'));
    request.onsuccess = () => {
      const item: unknown = request.result;
      if (item === undefined) { setResult(null); return; }
      if (!item || typeof item !== 'object') { reject(new Error('待核对请求格式无效。')); return; }
      const pending = item as Partial<PendingPairedWrite>;
      if (pending.deviceId !== deviceId || typeof pending.coreEpochId !== 'string'
        || !coreIdPattern.test(pending.coreEpochId)
        || typeof pending.requestId !== 'string' || pending.requestId.length < 1
        || pending.requestId.length > 300 || !/^[\x21-\x7e]+$/.test(pending.requestId)
        || !['add', 'remove'].includes(String(pending.operation))
        || typeof pending.payloadHash !== 'string' || !/^[0-9a-f]{64}$/.test(pending.payloadHash)) {
        reject(new Error('待核对请求格式无效。'));
        return;
      }
      setResult(pending as PendingPairedWrite);
    };
  });
}

export async function clearPendingPairedWrite(deviceId: string, expectedRequestId: string): Promise<void> {
  const database = await openDatabase();
  await transaction<void>(database, pendingStoreName, 'readwrite', (store, setResult, reject) => {
    const current = store.get(deviceId);
    current.onerror = () => reject(new Error('无法核对本机待处理请求。'));
    current.onsuccess = () => {
      if ((current.result as PendingPairedWrite | undefined)?.requestId !== expectedRequestId) {
        reject(new Error('待核对请求已变化，请刷新状态。'));
        return;
      }
      const removed = store.delete(deviceId);
      removed.onerror = () => reject(new Error('无法清除已核对的技能来源请求。'));
      removed.onsuccess = () => setResult();
    };
  });
}
