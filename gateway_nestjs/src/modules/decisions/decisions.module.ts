import { Module } from '@nestjs/common';

import { DecisionsController } from './decisions.controller';
import { DecisionsService } from './decisions.service';

@Module({
  controllers: [DecisionsController],
  providers: [DecisionsService],
})
export class DecisionsModule {}
