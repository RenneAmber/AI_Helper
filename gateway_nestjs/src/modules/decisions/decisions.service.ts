import { GoneException, Injectable, ServiceUnavailableException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import axios, { AxiosError } from 'axios';

@Injectable()
export class DecisionsService {
  private readonly baseUrl: string;
  private readonly internalToken: string;

  constructor(private readonly configService: ConfigService) {
    this.baseUrl = this.configService.get<string>('FASTAPI_BASE_URL', 'http://fastapi:8000');
    this.internalToken = this.configService.get<string>('INTERNAL_API_TOKEN', 'change-me-in-prod');
  }

  async createDecision(payload: Record<string, unknown>, traceId: string): Promise<Record<string, unknown>> {
    const normalizedPayload = this.normalizePayload(payload);
    try {
      const resp = await axios.post(`${this.baseUrl}/copilot/decisions`, normalizedPayload, {
        headers: { 'x-trace-id': traceId, 'x-internal-token': this.internalToken },
        timeout: 60000,
      });
      const data = resp.data as Record<string, unknown>;
      if (typeof data.decision_id === 'string' && typeof data.decisionId !== 'string') {
        data.decisionId = data.decision_id;
      }
      return data;
    } catch (error) {
      const err = error as AxiosError;
      throw new ServiceUnavailableException(`FastAPI decision create unavailable: ${err.message}`);
    }
  }

  async runDecision(decisionId: string, traceId: string): Promise<Record<string, unknown>> {
    throw new GoneException(`Decision run is obsolete for ${decisionId}. Use POST /decisions to execute the Copilot workflow in one request.`);
  }

  async replayDecision(decisionId: string, traceId: string): Promise<Record<string, unknown>> {
    throw new GoneException(`Decision replay is obsolete for ${decisionId}. The new Copilot workflow returns the final answer immediately.`);
  }

  private normalizePayload(payload: Record<string, unknown>): Record<string, unknown> {
    if ('problem_statement' in payload) {
      return payload;
    }

    const criteria = Array.isArray(payload.criteria) ? payload.criteria : [];
    const evaluationCriteria = Object.fromEntries(
      criteria
        .map((item) => {
          if (!item || typeof item !== 'object') {
            return null;
          }

          const key = String((item as { key?: unknown }).key ?? '').trim();
          const weight = Number((item as { weight?: unknown }).weight ?? 0);
          if (!key) {
            return null;
          }
          return [key, Number.isFinite(weight) ? weight : 0];
        })
        .filter((entry): entry is [string, number] => Array.isArray(entry)),
    );

    const requester = (payload.requester as { userId?: unknown } | undefined) ?? {};

    return {
      problem_statement: String(payload.question ?? payload.problem_statement ?? ''),
      domain: String(payload.domain ?? 'engineering'),
      evaluation_criteria: evaluationCriteria,
      user_id: String(requester.userId ?? payload.user_id ?? 'unknown'),
    };
  }
}
