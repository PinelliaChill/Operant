/**
 * Operant 2.0 Standard Error Code Taxonomy and Error Hierarchy
 */

export enum ErrorCode {
  VALIDATION_ERROR = 'VALIDATION_ERROR',
  NOT_FOUND = 'NOT_FOUND',
  CONFLICT = 'CONFLICT',
  UNAUTHORIZED = 'UNAUTHORIZED',
  POLICY_DENIED = 'POLICY_DENIED',
  APPROVAL_TIMEOUT = 'APPROVAL_TIMEOUT',
  APPROVAL_REVOKED = 'APPROVAL_REVOKED',
  CURSOR_EXPIRED = 'CURSOR_EXPIRED',
  SCHEMA_INCOMPATIBLE = 'SCHEMA_INCOMPATIBLE',
  HOST_OFFLINE = 'HOST_OFFLINE',
  RELAY_UNREACHABLE = 'RELAY_UNREACHABLE',
  COMMAND_REJECTED = 'COMMAND_REJECTED',
  CODER_REPLAY_BLOCKED = 'CODER_REPLAY_BLOCKED',
  RUNTIME_ERROR = 'RUNTIME_ERROR',
  INTERNAL_ERROR = 'INTERNAL_ERROR',
}

export interface ErrorDetails {
  code: ErrorCode;
  message: string;
  field?: string;
  action_hash?: string;
  cursor?: number;
  details?: Record<string, unknown>;
  recoverable: boolean;
  user_guidance?: string;
}

export class OperantError extends Error {
  readonly code: ErrorCode;
  readonly recoverable: boolean;
  readonly userGuidance?: string;
  readonly details?: Record<string, unknown>;

  constructor(options: ErrorDetails) {
    super(options.message);
    this.name = 'OperantError';
    this.code = options.code;
    this.recoverable = options.recoverable;
    this.userGuidance = options.user_guidance;
    this.details = options.details;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class NotFoundError extends OperantError {
  constructor(resource: string, id: string) {
    super({
      code: ErrorCode.NOT_FOUND,
      message: `${resource} with id '${id}' was not found.`,
      recoverable: false,
      user_guidance: 'Verify the identifier or refresh the projection list.',
    });
    this.name = 'NotFoundError';
  }
}

export class PolicyDeniedError extends OperantError {
  constructor(actionName: string, reason: string) {
    super({
      code: ErrorCode.POLICY_DENIED,
      message: `Action '${actionName}' was denied by security policy: ${reason}`,
      recoverable: false,
      user_guidance: 'Action cannot be executed. Policy DENY rules cannot be bypassed.',
    });
    this.name = 'PolicyDeniedError';
  }
}

export class CursorExpiredError extends OperantError {
  constructor(cursor: number) {
    super({
      code: ErrorCode.CURSOR_EXPIRED,
      message: `Event stream cursor ${cursor} has expired or is invalid.`,
      recoverable: true,
      user_guidance: 'Perform a full state query refresh to resynchronize your view.',
    });
    this.name = 'CursorExpiredError';
  }
}

export class HostOfflineError extends OperantError {
  constructor(hostId: string) {
    super({
      code: ErrorCode.HOST_OFFLINE,
      message: `Remote Host '${hostId}' is currently offline or unreachable.`,
      recoverable: true,
      user_guidance: 'Commands cannot produce side-effects while offline. Reconnect host to proceed.',
    });
    this.name = 'HostOfflineError';
  }
}
