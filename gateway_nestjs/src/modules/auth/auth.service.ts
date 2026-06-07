import { Injectable, UnauthorizedException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { JwtService } from '@nestjs/jwt';

@Injectable()
export class AuthService {
  constructor(
    private readonly jwtService: JwtService,
    private readonly configService: ConfigService,
  ) {}

  login(username: string, password: string): { access_token: string; token_type: string } {
    const expectedUser = this.configService.get<string>('DEMO_USER', 'admin');
    const expectedPass = this.configService.get<string>('DEMO_PASSWORD', 'admin123');

    if (username !== expectedUser || password !== expectedPass) {
      throw new UnauthorizedException('Invalid username or password');
    }

    const accessToken = this.jwtService.sign({ sub: username, username });
    return { access_token: accessToken, token_type: 'Bearer' };
  }
}
