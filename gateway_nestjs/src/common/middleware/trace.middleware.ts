import { Injectable, NestMiddleware } from '@nestjs/common';
import { randomUUID } from 'crypto';
import { NextFunction, Request, Response } from 'express';

interface TraceRequest extends Request {
  traceId?: string;
}

@Injectable()
export class TraceMiddleware implements NestMiddleware {
  use(req: TraceRequest, res: Response, next: NextFunction): void {
    const traceId = (req.headers['x-trace-id'] as string) || randomUUID();
    req.traceId = traceId;
    res.setHeader('x-trace-id', traceId);

    const startedAt = Date.now();
    res.on('finish', () => {
      const payload = {
        level: 'info',
        event: 'request.completed',
        trace_id: traceId,
        method: req.method,
        path: req.originalUrl,
        status_code: res.statusCode,
        latency_ms: Date.now() - startedAt,
      };
      console.log(JSON.stringify(payload));
    });

    next();
  }
}
