import {
  BadRequestException,
  Body,
  ConflictException,
  Controller,
  Get,
  Header,
  NotFoundException,
  Post,
  ServiceUnavailableException,
  UseGuards,
} from '@nestjs/common';

import { PermissionFlagType } from 'twenty-shared/constants';
import { ApiPath } from 'twenty-shared/types';

import { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';
import { AuthWorkspace } from 'src/engine/decorators/auth/auth-workspace.decorator';
import { AccessPolicyGuard } from 'src/engine/guards/access-policy.guard';

@Controller(`${ApiPath.Rest}/maystro/lead-collector`)
@UseGuards(
  AccessPolicyGuard({
    authPrincipals: {
      userSession: {
        standard: true,
        impersonated: false,
        playground: false,
        workspaceAgnostic: false,
      },
      apiKey: false,
      oauthClient: false,
      application: false,
    },
    requiredPermissionFlags: [PermissionFlagType.WORKSPACE],
  }),
)
export class MaystroLeadCollectorController {
  @Get('status')
  @Header('Cache-Control', 'no-store')
  status(@AuthWorkspace() workspace: WorkspaceEntity) {
    return this.requestCollector(workspace, 'status');
  }

  @Post('settings')
  saveSettings(
    @AuthWorkspace() workspace: WorkspaceEntity,
    @Body() settings: unknown,
  ) {
    return this.requestCollector(workspace, 'settings', settings);
  }

  @Post('run')
  run(@AuthWorkspace() workspace: WorkspaceEntity) {
    return this.requestCollector(workspace, 'run', {});
  }

  private async requestCollector(
    workspace: WorkspaceEntity,
    path: 'status' | 'settings' | 'run',
    body?: unknown,
  ): Promise<unknown> {
    const secret = process.env.LEADS_CONTROL_SECRET;
    if (!secret || workspace.id !== process.env.LEADS_WORKSPACE_ID) {
      throw new NotFoundException(
        'Lead collector is not configured for this workspace',
      );
    }

    const response = await fetch(`http://lead-collector:8765/${path}`, {
      method: path === 'status' ? 'GET' : 'POST',
      headers: {
        Authorization: `Bearer ${secret}`,
        'Content-Type': 'application/json',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(5_000),
      redirect: 'error',
    }).catch(() => {
      throw new ServiceUnavailableException('Lead collector is unavailable');
    });

    if (response.status === 400) {
      throw new BadRequestException('Invalid collector settings');
    }
    if (response.status === 409) {
      throw new ConflictException(
        'Lead collector is unavailable or already running',
      );
    }
    if (!response.ok) {
      throw new ServiceUnavailableException('Lead collector is unavailable');
    }

    return response.json();
  }
}
