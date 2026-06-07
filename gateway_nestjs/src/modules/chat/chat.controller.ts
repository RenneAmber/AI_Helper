import { Body, Controller, Get, Post, Req, Res, UseGuards } from '@nestjs/common';
import { Request, Response } from 'express';

import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { ChatDto } from './dto/chat.dto';
import { ChatService } from './chat.service';

interface RequestUser {
  userId: string;
  username: string;
}

interface AuthenticatedRequest extends Request {
  user: RequestUser;
  traceId?: string;
}

@Controller('chat')
@UseGuards(JwtAuthGuard)
export class ChatController {
  constructor(private readonly chatService: ChatService) {}

  @Post()
  async chat(@Body() dto: ChatDto, @Req() req: AuthenticatedRequest, @Res() res: Response): Promise<void> {
    const traceId = req.traceId || 'missing-trace-id';

    if (dto.stream) {
      const upstream = await this.chatService.chatStream(dto, req.user, traceId);
      res.setHeader('Content-Type', 'text/event-stream');
      res.setHeader('Cache-Control', 'no-cache');
      res.setHeader('Connection', 'keep-alive');
      res.setHeader('x-trace-id', traceId);
      upstream.pipe(res);
      return;
    }

    const response = await this.chatService.chat(dto, req.user, traceId);
    res.status(200).json(response);
  }

  @Get('replay/:traceId')
  async replay(@Req() req: AuthenticatedRequest, @Res() res: Response): Promise<void> {
    const traceIdRaw = req.params.traceId;
    const traceId = Array.isArray(traceIdRaw) ? traceIdRaw[0] : traceIdRaw;
    const data = await this.chatService.replay(traceId, req.traceId || 'missing-trace-id');
    res.status(200).json(data);
  }
}
