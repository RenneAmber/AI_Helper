import { IsBoolean, IsOptional, IsString, MaxLength, MinLength } from 'class-validator';

export class ChatDto {
  @IsString()
  @MinLength(1)
  @MaxLength(5000)
  message!: string;

  @IsString()
  @IsOptional()
  @MaxLength(128)
  session_id?: string;

  @IsBoolean()
  @IsOptional()
  stream?: boolean;

  @IsBoolean()
  @IsOptional()
  force_fail?: boolean;
}
