import { Body, Controller, Get, Param, Post, Req, UseGuards } from '@nestjs/common';
import { Request } from 'express';

import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { DecisionsService } from './decisions.service';

interface RequestUser {
  userId: string;
  username: string;
}

interface AuthenticatedRequest extends Request {
  user: RequestUser;
  traceId?: string;
}

@Controller('decisions')
@UseGuards(JwtAuthGuard)
export class DecisionsController {
  constructor(private readonly decisionsService: DecisionsService) {}

  @Post()
  async create(@Body() payload: Record<string, unknown>, @Req() req: AuthenticatedRequest): Promise<Record<string, unknown>> {
    const traceId = req.traceId || 'missing-trace-id';
    return this.decisionsService.createDecision(payload, traceId);
  }

  @Post(':decisionId/run')
  async run(@Param('decisionId') decisionId: string, @Req() req: AuthenticatedRequest): Promise<Record<string, unknown>> {
    const traceId = req.traceId || 'missing-trace-id';
    return this.decisionsService.runDecision(decisionId, traceId);
  }

  @Get(':decisionId/replay')
  async replay(@Param('decisionId') decisionId: string, @Req() req: AuthenticatedRequest): Promise<Record<string, unknown>> {
    const traceId = req.traceId || 'missing-trace-id';
    return this.decisionsService.replayDecision(decisionId, traceId);
  }
}
