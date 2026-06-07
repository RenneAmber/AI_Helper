import { MiddlewareConsumer, Module, NestModule } from '@nestjs/common';
import { ConfigModule, ConfigService } from '@nestjs/config';
import { APP_GUARD } from '@nestjs/core';
import { ThrottlerGuard, ThrottlerModule } from '@nestjs/throttler';

import { TraceMiddleware } from '../common/middleware/trace.middleware';
import { AuthModule } from './auth/auth.module';
import { ChatModule } from './chat/chat.module';
import { DecisionsModule } from './decisions/decisions.module';

@Module({
  imports: [
    ConfigModule.forRoot({ isGlobal: true }),
    ThrottlerModule.forRootAsync({
      inject: [ConfigService],
      useFactory: (cfg: ConfigService) => [
        {
          ttl: Number(cfg.get('THROTTLE_TTL', 60)) * 1000,
          limit: Number(cfg.get('THROTTLE_LIMIT', 30)),
        },
      ],
    }),
    AuthModule,
    ChatModule,
    DecisionsModule,
  ],
  providers: [
    {
      provide: APP_GUARD,
      useClass: ThrottlerGuard,
    },
  ],
})
export class AppModule implements NestModule {
  configure(consumer: MiddlewareConsumer): void {
    consumer.apply(TraceMiddleware).forRoutes('*');
  }
}
