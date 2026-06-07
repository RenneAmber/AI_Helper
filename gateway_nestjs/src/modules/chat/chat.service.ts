import { Injectable, ServiceUnavailableException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import axios, { AxiosError } from 'axios';
import { randomUUID } from 'crypto';
import { Readable } from 'stream';

import { ChatDto } from './dto/chat.dto';

interface UserInfo {
  userId: string;
  username: string;
}

@Injectable()
export class ChatService {
  private readonly baseUrl: string;
  private readonly internalToken: string;

  constructor(private readonly configService: ConfigService) {
    this.baseUrl = this.configService.get<string>('FASTAPI_BASE_URL', 'http://fastapi:8000');
    this.internalToken = this.configService.get<string>('INTERNAL_API_TOKEN', 'change-me-in-prod');
  }

  private normalizePayload(
    dto: ChatDto,
    user: UserInfo,
  ): { message: string; session_id: string; user_id: string; force_fail: boolean } {
    return {
      message: dto.message,
      session_id: dto.session_id || randomUUID(),
      user_id: user.userId,
      force_fail: Boolean(dto.force_fail),
    };
  }

  async chat(
    dto: ChatDto,
    user: UserInfo,
    traceId: string,
  ): Promise<{ session_id: string; answer: string; trace_id: string; evidence: Array<Record<string, unknown>> }> {
    const payload = this.normalizePayload(dto, user);

    try {
      const resp = await axios.post(`${this.baseUrl}/internal/chat`, payload, {
        headers: { 'x-internal-token': this.internalToken, 'x-trace-id': traceId },
        timeout: 15000,
      });
      return resp.data;
    } catch (error) {
      const err = error as AxiosError;
      throw new ServiceUnavailableException(`FastAPI unavailable: ${err.message}`);
    }
  }

  async chatStream(dto: ChatDto, user: UserInfo, traceId: string): Promise<Readable> {
    const payload = this.normalizePayload(dto, user);

    try {
      const resp = await axios.post(`${this.baseUrl}/internal/chat/stream`, payload, {
        headers: { 'x-internal-token': this.internalToken, 'x-trace-id': traceId },
        responseType: 'stream',
        timeout: 30000,
      });
      return resp.data as Readable;
    } catch (error) {
      const err = error as AxiosError;
      throw new ServiceUnavailableException(`FastAPI stream unavailable: ${err.message}`);
    }
  }

  async replay(traceId: string, gatewayTraceId: string): Promise<Record<string, unknown>> {
    try {
      const resp = await axios.get(`${this.baseUrl}/internal/chat/replay/${traceId}`, {
        headers: { 'x-internal-token': this.internalToken, 'x-trace-id': gatewayTraceId },
        timeout: 15000,
      });
      return resp.data as Record<string, unknown>;
    } catch (error) {
      const err = error as AxiosError;
      throw new ServiceUnavailableException(`FastAPI replay unavailable: ${err.message}`);
    }
  }
}
