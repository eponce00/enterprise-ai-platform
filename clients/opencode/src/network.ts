export function withRequestTimeout(timeoutMs: number, callerSignal?: AbortSignal | null): AbortSignal {
  const timeoutSignal = AbortSignal.timeout(timeoutMs)
  return callerSignal ? AbortSignal.any([callerSignal, timeoutSignal]) : timeoutSignal
}
