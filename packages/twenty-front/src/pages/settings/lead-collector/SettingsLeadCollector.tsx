import { REST_API_BASE_URL } from '@/apollo/constants/RestApiBaseUrl';
import { SettingsPageContainer } from '@/settings/components/SettingsPageContainer';
import { SettingsPageLayout } from '@/settings/components/layout/SettingsPageLayout';
import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { SESSION_COOKIE_OPT_IN_HEADER_NAME } from 'twenty-shared/constants';
import { SettingsPath } from 'twenty-shared/types';
import { getSettingsPath } from 'twenty-shared/utils';
import { Section } from 'twenty-ui/components/layout';
import { IconPlayerPlay } from 'twenty-ui/icon';
import { Button, Switch } from 'twenty-ui/primitives/input';
import { themeCssVariables } from 'twenty-ui/theme';

type CollectorStatus = {
  ready: boolean;
  busy: boolean;
  imported: number;
  pending: number;
  settings: { enabled: boolean; keywords: string[]; minScore: number };
  lastRun: {
    status: 'running' | 'completed' | 'failed' | 'interrupted';
    startedAt: string;
    finishedAt: string | null;
  } | null;
};

type CollectorDraft = { enabled: boolean; keywords: string; minScore: string };

const StyledRow = styled.div`
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: ${themeCssVariables.spacing[3]};
  justify-content: space-between;
`;

const StyledForm = styled.form`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[4]};
`;

const StyledField = styled.label`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledKeywords = styled.textarea`
  background: transparent;
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.sm};
  box-sizing: border-box;
  color: inherit;
  font: inherit;
  padding: ${themeCssVariables.spacing[2]};
  resize: vertical;
  width: 100%;
`;

const StyledScore = styled.input`
  background: transparent;
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: inherit;
  font: inherit;
  padding: ${themeCssVariables.spacing[2]};
  width: 64px;
`;

const StyledMuted = styled.span`
  color: ${themeCssVariables.font.color.secondary};
`;

const requestCollector = async (
  path: 'status' | 'settings' | 'run',
  options: { body?: CollectorStatus['settings']; signal?: AbortSignal } = {},
): Promise<CollectorStatus> => {
  const response = await fetch(
    `${REST_API_BASE_URL}/maystro/lead-collector/${path}`,
    {
      method: path === 'status' ? 'GET' : 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        [SESSION_COOKIE_OPT_IN_HEADER_NAME]: '1',
      },
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    },
  );
  if (!response.ok) {
    throw new Error(String(response.status));
  }
  return response.json();
};

export const SettingsLeadCollector = () => {
  const { t } = useLingui();
  const [status, setStatus] = useState<CollectorStatus>();
  const [unavailable, setUnavailable] = useState(false);
  const [draft, setDraft] = useState<CollectorDraft>();
  const [action, setAction] = useState<'settings' | 'run'>();
  const [message, setMessage] = useState<{ text: string; isError: boolean }>();

  useEffect(() => {
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const nextStatus = await requestCollector('status', {
          signal: controller.signal,
        });
        setStatus(nextStatus);
        setUnavailable(false);
      } catch {
        if (!controller.signal.aborted) {
          setUnavailable(true);
        }
      }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5_000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, []);

  const form =
    draft ??
    (status && {
      enabled: status.settings.enabled,
      keywords: status.settings.keywords.join('\n'),
      minScore: String(status.settings.minScore),
    });

  const submit = async (path: 'settings' | 'run') => {
    setMessage(undefined);
    const settings =
      path === 'settings' && form
        ? {
            enabled: form.enabled,
            keywords: form.keywords
              .split('\n')
              .map((keyword) => keyword.trim())
              .filter(Boolean),
            minScore: Number(form.minScore),
          }
        : undefined;
    if (
      settings &&
      (settings.keywords.length < 1 ||
        settings.keywords.length > 10 ||
        settings.keywords.some((keyword) => keyword.length > 100) ||
        !Number.isInteger(settings.minScore) ||
        settings.minScore < 1 ||
        settings.minScore > 5)
    ) {
      setMessage({
        text: t`Enter 1 to 10 keywords, up to 100 characters each, and a score from 1 to 5.`,
        isError: true,
      });
      return;
    }

    setAction(path);
    try {
      setStatus(await requestCollector(path, { body: settings }));
      if (path === 'settings') {
        setDraft(undefined);
      }
      setMessage({
        text:
          path === 'settings'
            ? t`Collector settings saved.`
            : t`Collection started.`,
        isError: false,
      });
    } catch (error) {
      setMessage({
        text:
          error instanceof Error && error.message === '409'
            ? t`The collector is already running or unavailable.`
            : t`The collector could not complete this request. Try again.`,
        isError: true,
      });
    } finally {
      setAction(undefined);
    }
  };

  const runLabels = {
    running: t`Running`,
    completed: t`Completed`,
    failed: t`Failed`,
    interrupted: t`Interrupted`,
  };

  return (
    <SettingsPageLayout
      title={t`Lead collector`}
      links={[
        { children: t`Workspace`, href: getSettingsPath(SettingsPath.General) },
        { children: t`Lead collector` },
      ]}
    >
      <SettingsPageContainer>
        {unavailable && (
          <p role="alert">{t`The collector is unavailable. This page will retry automatically.`}</p>
        )}
        {!status && !unavailable && (
          <p role="status">{t`Loading collector status…`}</p>
        )}
        {status && form && (
          <>
            <Section.Root>
              <Section.Header
                title={t`Activity`}
                description={t`Collect qualified leads from Meta Ad Library and send them to Opportunities.`}
              />
              <StyledRow>
                <span role="status">
                  {!status.ready
                    ? t`API key required`
                    : status.busy
                      ? t`Collecting leads…`
                      : status.settings.enabled
                        ? t`Scheduled`
                        : t`Schedule paused`}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  startIcon={<IconPlayerPlay />}
                  disabled={
                    unavailable ||
                    !status.ready ||
                    status.busy ||
                    action !== undefined
                  }
                  loading={action === 'run'}
                  onClick={() => void submit('run')}
                >
                  {t`Run now`}
                </Button>
              </StyledRow>
              <StyledRow>
                <StyledMuted>{t`${status.imported} imported · ${status.pending} pending`}</StyledMuted>
                <Link to="/objects/opportunities">{t`View opportunities`}</Link>
              </StyledRow>
              <StyledMuted>
                {status.lastRun
                  ? t`Last run: ${runLabels[status.lastRun.status]} · ${new Date(status.lastRun.startedAt).toLocaleString()}`
                  : t`No run recorded yet.`}
              </StyledMuted>
            </Section.Root>
            <Section.Root>
              <Section.Header
                title={t`Collection settings`}
                description={t`Scheduled runs start daily at 08:00 Tunisia time.`}
              />
              <StyledForm
                onSubmit={(event) => {
                  event.preventDefault();
                  void submit('settings');
                }}
              >
                <StyledRow>
                  <label htmlFor="lead-collector-enabled">{t`Daily collection`}</label>
                  <Switch
                    id="lead-collector-enabled"
                    size="sm"
                    checked={form.enabled}
                    disabled={unavailable || action !== undefined}
                    onCheckedChange={(enabled) =>
                      setDraft({ ...form, enabled })
                    }
                  />
                </StyledRow>
                <StyledField>
                  {t`Search keywords`}
                  <StyledMuted>{t`Enter one search phrase per line.`}</StyledMuted>
                  <StyledKeywords
                    rows={4}
                    dir="auto"
                    value={form.keywords}
                    disabled={unavailable || action !== undefined}
                    onChange={(event) =>
                      setDraft({ ...form, keywords: event.currentTarget.value })
                    }
                  />
                </StyledField>
                <StyledField>
                  {t`Minimum qualification score`}
                  <StyledScore
                    type="number"
                    min={1}
                    max={5}
                    required
                    value={form.minScore}
                    disabled={unavailable || action !== undefined}
                    onChange={(event) =>
                      setDraft({ ...form, minScore: event.currentTarget.value })
                    }
                  />
                </StyledField>
                <StyledRow>
                  <Button
                    type="submit"
                    color="accent"
                    variant="solid"
                    size="sm"
                    loading={action === 'settings'}
                    disabled={!draft || unavailable || action !== undefined}
                  >{t`Save settings`}</Button>
                </StyledRow>
              </StyledForm>
            </Section.Root>
            {message && (
              <p role={message.isError ? 'alert' : 'status'}>{message.text}</p>
            )}
          </>
        )}
      </SettingsPageContainer>
    </SettingsPageLayout>
  );
};
