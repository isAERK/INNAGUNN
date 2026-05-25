import './style.css';
import { invoke } from '@tauri-apps/api/core';
import { appCacheDir, documentDir, join } from '@tauri-apps/api/path';
import { open as pickFolder } from '@tauri-apps/plugin-dialog';

type Profile = {
  school?: string;
  name?: string;
  status?: string;
  row_text?: string;
  direct?: boolean;
};

type Term = {
  term_code?: string;
  term_name?: string;
  courses?: Array<{ viewable?: boolean }>;
};

type Status = {
  logged_in?: boolean;
  profile_selector_ready?: boolean;
  profiles_count?: number;
  profiles?: Profile[];
  current_url?: string;
  page_url?: string;
  school_short?: string;
  user_name?: string;
};

const app = document.querySelector<HTMLDivElement>('#app')!;

const state = {
  output: 'INNA_Archive',
  selectedProfile: '',
  activeDirectProfile: false,
  profiles: [] as Profile[],
  terms: [] as Term[],
  selectedTerms: new Set<string>(),
  running: false,
  stopRequested: false,
  commandBusy: false,
  loginWatcherTimer: 0 as number | ReturnType<typeof setInterval>,
  loginWatcherStartedAt: 0,
  loginAutoProfileFetched: false,
  privacyMode: false,
  includeClassmates: true,
  renderPdf: false,
  includeMedia: true,
  debugApi: false,
  debugDownloads: false,
  assignmentDebug: false,
  log: '',
  sidecarLogFile: '',
  sidecarLastLogLength: 0,
  sidecarEventsFile: '',
  sidecarLastEventsLength: 0,
  logPollTimer: 0 as number | ReturnType<typeof setInterval>,
  eventsPollTimer: 0 as number | ReturnType<typeof setInterval>,
  progress: {
    step: 'Idle',
    courseText: 'No course running',
    materialsText: 'No materials yet',
    assignmentsText: 'No assignments yet',
    coursePct: 0,
    materialsPct: 0,
    assignmentsPct: 0
  },
  ownDataConfirmed: false,
  showDisclaimerDetails: false
};

const DETAILED_DISCLAIMER = `
Ég staðfesti að ég er að sækja mín eigin gögn úr INNU, eða gögn sem ég hef skýrt leyfi til að sækja fyrir viðkomandi aðila.

Ég skil að safnið getur innihaldið viðkvæm gögn, svo sem einkunnir, athugasemdir kennara, skilaverkefni og nöfn/netföng annarra nemenda ef Hópalisti er sóttur.

Ég mun ekki deila safninu, Hópalista, verkefnum, einkunnum eða öðrum persónuupplýsingum án heimildar.`;

function showModalElement(selector: string, show: boolean) {
  const el = document.querySelector<HTMLElement>(selector);
  if (!el) return;
  el.classList.toggle('hidden', !show);
  el.toggleAttribute('hidden', !show);
  el.style.display = show ? '' : 'none';
}

function openDisclaimerModal() {
  state.showDisclaimerDetails = true;
  showModalElement('#disclaimerModal', true);
  showModalElement('#disclaimerBackdrop', true);
}

function closeDisclaimerModal() {
  state.showDisclaimerDetails = false;
  showModalElement('#disclaimerModal', false);
  showModalElement('#disclaimerBackdrop', false);
}

async function initializeDefaultOutput() {
  try {
    const docs = await documentDir();
    state.output = await join(docs, 'INNA_Archive');
    render();
  } catch (error) {
    appendLog(`Could not resolve Documents folder, using relative output path: ${String(error)}`);
  }
}

function baseArgs(): string[] {
  return [
    '--connect-cdp', 'http://127.0.0.1:9222',
    '--base-url', 'https://nam.inna.is',
    '--out', state.output,
    '--download-stall-timeout-ms', '5000',
    '--download-timeout-ms', '1800000',
    '--api-timeout-ms', '20000',
    '--progress-every', '10'
  ];
}

async function ensureSidecarLogFile(): Promise<string> {
  if (state.sidecarLogFile) return state.sidecarLogFile;
  const cache = await appCacheDir();
  state.sidecarLogFile = await join(cache, `inna-sidecar-${Date.now()}.log`);
  state.sidecarLastLogLength = 0;
  return state.sidecarLogFile;
}

async function ensureSidecarEventsFile(): Promise<string> {
  if (state.sidecarEventsFile) return state.sidecarEventsFile;
  const cache = await appCacheDir();
  state.sidecarEventsFile = await join(cache, `inna-sidecar-${Date.now()}.events.jsonl`);
  state.sidecarLastEventsLength = 0;
  return state.sidecarEventsFile;
}

function resetProgressState() {
  state.progress.step = 'Starting…';
  state.progress.courseText = 'Waiting for course events…';
  state.progress.materialsText = 'Waiting for material events…';
  state.progress.assignmentsText = 'Waiting for assignment events…';
  state.progress.coursePct = 0;
  state.progress.materialsPct = 0;
  state.progress.assignmentsPct = 0;
}

function progressLabel(current: number, total: number, fallback = ''): string {
  if (!total) return fallback || '0 / 0';
  return `${current} / ${total}`;
}

function pct(current: number, total: number): number {
  if (!total) return 100;
  return Math.max(0, Math.min(100, (current / total) * 100));
}

function setProgressDom() {
  setBar('courseBar', state.progress.coursePct);
  setBar('materialsBar', state.progress.materialsPct);
  setBar('assignmentsBar', state.progress.assignmentsPct);

  const step = document.querySelector<HTMLElement>('#progressStep');
  if (step) step.textContent = state.progress.step;

  const course = document.querySelector<HTMLElement>('#courseProgressText');
  if (course) course.textContent = state.progress.courseText;

  const materials = document.querySelector<HTMLElement>('#materialsProgressText');
  if (materials) materials.textContent = state.progress.materialsText;

  const assignments = document.querySelector<HTMLElement>('#assignmentsProgressText');
  if (assignments) assignments.textContent = state.progress.assignmentsText;
}

function handleProgressEvent(event: any) {
  const type = event?.type || '';

  if (type === 'sidecar_started') {
    state.progress.step = `Starting ${event.command || 'sidecar'}…`;
  } else if (type === 'run_start') {
    state.progress.step = `Preparing ${event.total_courses || 0} course(s)…`;
    state.progress.courseText = `0 / ${event.total_courses || 0} courses`;
    state.progress.coursePct = 0;
  } else if (type === 'course_start') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    const name = event.course_name || event.course_code || 'course';
    state.progress.step = `Course ${progressLabel(current, total)}: ${name}`;
    state.progress.courseText = `${progressLabel(Math.max(0, current - 1), total)} complete · now ${name}`;
    state.progress.coursePct = pct(Math.max(0, current - 1), total);
    state.progress.materialsText = 'Waiting for materials…';
    state.progress.assignmentsText = 'Waiting for assignments…';
    state.progress.materialsPct = 0;
    state.progress.assignmentsPct = 0;
  } else if (type === 'course_metadata_start') {
    state.progress.step = `Fetching metadata for ${event.course_code || 'course'}…`;
  } else if (type === 'course_metadata_done') {
    state.progress.step = `Metadata ready: ${event.course || event.course_code || 'course'}`;
  } else if (type === 'materials_start') {
    const total = Number(event.total || 0);
    state.progress.step = `Downloading / recording materials for ${event.course || 'course'}…`;
    state.progress.materialsText = `0 / ${total} materials`;
    state.progress.materialsPct = total ? 0 : 100;
  } else if (type === 'material_current') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.step = `Material ${progressLabel(current, total)}: ${event.name || ''}`;
    state.progress.materialsText = `${progressLabel(Math.max(0, current - 1), total)} complete · now ${event.name || 'material'}`;
    state.progress.materialsPct = pct(Math.max(0, current - 1), total);
  } else if (type === 'material_progress') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.materialsText = `${progressLabel(current, total)} materials`;
    state.progress.materialsPct = pct(current, total);
  } else if (type === 'materials_done') {
    const total = Number(event.total || 0);
    state.progress.materialsText = `${progressLabel(total, total)} materials done`;
    state.progress.materialsPct = 100;
  } else if (type === 'assignments_start') {
    const total = Number(event.total || 0);
    state.progress.step = `Processing assignments/tests for ${event.course || 'course'}…`;
    state.progress.assignmentsText = `0 / ${total} assignments/tests`;
    state.progress.assignmentsPct = total ? 0 : 100;
  } else if (type === 'assignment_current') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.step = `Assignment ${progressLabel(current, total)}: ${event.title || event.assignment_id || ''}`;
    state.progress.assignmentsText = `${progressLabel(Math.max(0, current - 1), total)} complete · now ${event.title || event.assignment_id || 'assignment'}`;
    state.progress.assignmentsPct = pct(Math.max(0, current - 1), total);
  } else if (type === 'assignment_endpoints_done') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.assignmentsText = `${progressLabel(Math.max(0, current - 1), total)} complete · fetched endpoints for ${event.assignment_id || 'assignment'}`;
  } else if (type === 'assignment_progress') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.assignmentsText = `${progressLabel(current, total)} assignments/tests`;
    state.progress.assignmentsPct = pct(current, total);
  } else if (type === 'assignment_error') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.step = `Assignment error ${progressLabel(current, total)}: ${event.title || event.assignment_id || ''}`;
    state.progress.assignmentsText = `${progressLabel(current, total)} assignments/tests · error recorded`;
    state.progress.assignmentsPct = pct(current, total);
    appendLog(`Assignment error recorded: ${event.title || event.assignment_id || ''} — ${event.error || ''}`);
  } else if (type === 'assignments_done') {
    const total = Number(event.total || 0);
    state.progress.assignmentsText = `${progressLabel(total, total)} assignments/tests done`;
    state.progress.assignmentsPct = 100;
  } else if (type === 'course_done') {
    const current = Number(event.current || 0);
    const total = Number(event.total || 0);
    state.progress.step = `Finished course ${progressLabel(current, total)}: ${event.course_name || event.course_code || ''}`;
    state.progress.courseText = `${progressLabel(current, total)} courses done`;
    state.progress.coursePct = pct(current, total);
  } else if (type === 'course_error') {
    state.progress.step = `Course failed: ${event.course_code || ''} ${event.error || ''}`;
  } else if (type === 'course_failed_before_index') {
    state.progress.step = `Course failed before viewer index: ${event.course_code || ''} ${event.error || ''}`;
    appendLog(`Course failed before it could be added to archive_index.json: ${event.course_code || ''} — ${event.error || ''}`);
  } else if (type === 'done') {
    state.progress.step = `Done. Archive root: ${event.archive_root || ''}`;
    state.progress.coursePct = 100;
    state.progress.materialsPct = 100;
    state.progress.assignmentsPct = 100;
  }

  setProgressDom();
}

async function importSidecarEvents(label = '') {
  try {
    const eventsPath = await ensureSidecarEventsFile();
    const text = await invoke<string>('read_text_file', { path: eventsPath });
    if (!text) return;

    const nextChunk = text.slice(state.sidecarLastEventsLength);
    if (!nextChunk) return;

    state.sidecarLastEventsLength = text.length;

    if (label) appendLog(`\n--- ${label} ---`);
    for (const line of nextChunk.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        handleProgressEvent(JSON.parse(trimmed));
      } catch (error) {
        appendLog(`Progress event parse failed: ${String(error)} :: ${trimmed.slice(0, 200)}`);
      }
    }
    if (label) appendLog(`--- end ${label} ---\n`);
  } catch (_error) {
    // The events file may not exist yet. Ignore while the sidecar starts.
  }
}

async function startEventPolling() {
  await ensureSidecarEventsFile();
  if (state.eventsPollTimer) clearInterval(state.eventsPollTimer as any);
  state.eventsPollTimer = setInterval(() => {
    importSidecarEvents();
  }, 300);
}

function stopEventPolling() {
  if (state.eventsPollTimer) {
    clearInterval(state.eventsPollTimer as any);
    state.eventsPollTimer = 0 as any;
  }
}

async function importSidecarLog(label = '') {
  try {
    const logPath = await ensureSidecarLogFile();
    const text = await invoke<string>('read_text_file', { path: logPath });
    if (!text) return;

    const nextChunk = text.slice(state.sidecarLastLogLength);
    if (!nextChunk) return;

    state.sidecarLastLogLength = text.length;

    if (label) appendLog(`\n--- ${label} ---`);
    for (const line of nextChunk.split(/\r?\n/)) {
      if (line.trim()) {
        appendLog(line);
      }
    }
    if (label) appendLog(`--- end ${label} ---\n`);
  } catch (_error) {
    // The log file may not exist yet. Ignore while the sidecar starts.
  }
}

async function startLogPolling() {
  await ensureSidecarLogFile();
  if (state.logPollTimer) clearInterval(state.logPollTimer as any);

  state.logPollTimer = setInterval(() => {
    importSidecarLog();
  }, 500);
}

function stopLogPolling() {
  if (state.logPollTimer) {
    clearInterval(state.logPollTimer as any);
    state.logPollTimer = 0 as any;
  }
}

async function appendSidecarLogTail(label = 'sidecar log') {
  try {
    const logPath = await ensureSidecarLogFile();
    const text = await invoke<string>('read_text_file', { path: logPath });
    const tail = text.split(/\r?\n/).slice(-80).join('\n').trim();
    if (tail) {
      appendLog(`\n--- ${label} tail ---`);
      appendLog(tail);
      appendLog(`--- end ${label} tail ---\n`);
    }
  } catch (_error) {
    // Best-effort only.
  }
}

function privacyArgs(): string[] {
  const args: string[] = [];
  if (state.privacyMode) args.push('--privacy-mode');
  if (!state.includeClassmates) args.push('--skip-classmates');
  if (state.renderPdf) args.push('--render-pdf');
  if (!state.includeMedia) args.push('--skip-media');
  if (state.debugApi) args.push('--verbose-api');
  if (state.debugDownloads) args.push('--verbose-downloads');
  if (state.assignmentDebug) args.push('--assignment-debug');
  return args;
}

function appendLog(line: string) {
  state.log += line.endsWith('\n') ? line : line + '\n';
  const log = document.querySelector<HTMLTextAreaElement>('#log');
  if (log) {
    log.value = state.log;
    log.scrollTop = log.scrollHeight;
  }
  updateProgressFromLine(line);
}


function profileLabel(p: Profile, index = 0): string {
  return p.school || p.row_text || `Profile ${index + 1}`;
}

function applyProfiles(profiles: Profile[], source = 'profiles') {
  state.profiles = profiles || [];
  if (state.profiles.length > 0) {
    const stillExists = state.profiles.some((p, i) => profileLabel(p, i) === state.selectedProfile);
    if (!state.selectedProfile || !stillExists) {
      state.selectedProfile = '';
      state.activeDirectProfile = false;
    }
    appendLog(`Loaded ${state.profiles.length} school/profile option(s) from ${source}. Choose one from the dropdown.`);
  }
  render();
}

function synthesizeProfileFromStatus(status: Status): boolean {
  if (!status.logged_in) return false;

  const school = status.school_short
    ? `Current active INNA session · ${status.school_short}`
    : 'Current active INNA session';

  state.profiles = [{
    school,
    name: status.user_name || 'Already inside INNA',
    status: 'Active one-school/direct session',
    row_text: school,
    direct: true
  }];

  state.selectedProfile = school;
  state.activeDirectProfile = true;
  appendLog(`Using ${school} from active INNA session.`);
  render();
  return true;
}

function statusClass(status: Status): string {
  if (!status.logged_in && !status.profile_selector_ready) return 'red';
  if (canDownload()) return 'green';
  return 'yellow';
}

function statusText(status: Status): string {
  if (!status.logged_in && !status.profile_selector_ready) return 'Login needed or Chrome not reachable';
  if (status.profile_selector_ready && !status.logged_in) return `Profile selector ready${status.profiles_count ? ` (${status.profiles_count} profiles)` : ''}`;
  if (!state.selectedProfile) return 'INNA session active, choose school/profile';
  if (state.terms.length === 0) return 'School selected, refresh semesters';
  return canDownload() ? 'INNA ready · Ready to download' : `INNA ready · ${readinessText()}`;
}

async function sidecarExecute(args: string[]): Promise<{ code: number | null }> {
  const logFile = await ensureSidecarLogFile();
  const eventsFile = await ensureSidecarEventsFile();
  const fullArgs = [...args, '--log-file', logFile, '--events-file', eventsFile];
  await startLogPolling();
  await startEventPolling();

  // Run through a Rust command. The Rust side redirects output to null and
  // the GUI reads the UTF-8 log file and JSONL events file instead.
  const code = await invoke<number>('run_downloader', { args: fullArgs });
  await importSidecarLog('final sidecar log');
  await importSidecarEvents('final progress events');
  return { code };
}

async function sidecarJson(commandName: 'status' | 'profiles' | 'catalog'): Promise<any> {
  if (state.commandBusy && commandName === 'status') {
    throw new Error('Busy with another INNA operation');
  }

  state.commandBusy = true;
  const cache = await appCacheDir();
  const path = await join(cache, `inna-${commandName}-${Date.now()}.json`);
  const jsonFlag = commandName === 'status' ? '--status-json' : commandName === 'profiles' ? '--profiles-json' : '--catalog-json';

  const args = [commandName, ...baseArgs(), jsonFlag, path];
  if (commandName === 'catalog' && state.selectedProfile && !state.activeDirectProfile) {
    args.push('--school', state.selectedProfile, '--force-school-select');
  }

  appendLog(`$ inna_downloader_cli ${args.join(' ')}\n`);
  try {
    const result = await sidecarExecute(args);
    if (result.code !== 0) {
      await appendSidecarLogTail(`${commandName} failure`);
      throw new Error(`Downloader exited with code ${result.code}`);
    }
    const text = await invoke<string>('read_text_file', { path });
    return JSON.parse(text);
  } catch (error) {
    await appendSidecarLogTail(`${commandName} error`);
    throw error;
  } finally {
    state.commandBusy = false;
  }
}

async function startChrome() {
  try {
    const msg = await invoke<string>('start_chrome');
    appendLog(msg);
    startLoginWatcher();
  } catch (error) {
    appendLog(`Could not start Chrome: ${String(error)}`);
    setLoginStatusUi('red', `Could not start Chrome: ${String(error)}`);
  }
}

async function checkStatus() {
  await checkStatusOnce('manual');
}

async function refreshProfiles() {
  stopLoginWatcher();
  // First ask the lightweight status endpoint. If it sees the selector page,
  // it already includes the two school rows. This avoids repeatedly navigating
  // the user's browser to r.inna.is/adgangur.
  try {
    const status = await sidecarJson('status') as Status;
    if (status.profile_selector_ready && (status.profiles || []).length > 0) {
      applyProfiles(status.profiles || [], 'status/profile selector');
      return;
    }
    if (status.logged_in && !status.profile_selector_ready) {
      synthesizeProfileFromStatus(status);
      return;
    }
  } catch (error) {
    appendLog(`Fast profile check failed: ${String(error)}`);
  }

  let data: any = { profiles: [] };
  try {
    data = await sidecarJson('profiles');
  } catch (error) {
    appendLog(`Profile list read failed: ${String(error)}`);
    appendLog('Trying direct-session fallback from login status...');
    try {
      const status = await sidecarJson('status') as Status;
      if (synthesizeProfileFromStatus(status)) {
        render();
        return;
      }
    } catch (statusError) {
      appendLog(`Direct-session fallback failed: ${String(statusError)}`);
    }
  }

  state.profiles = data.profiles || [];

  if (state.profiles.length === 0) {
    // Some users only have one INNA school profile. INNA then skips r.inna.is/adgangur
    // and sends them directly into the school instance, so there is no selector page to list.
    try {
      const status = await sidecarJson('status') as Status;
      if (status.logged_in) {
        if (synthesizeProfileFromStatus(status)) {
          appendLog(`Using ${state.selectedProfile} from active INNA session.`);
        }
      }
    } catch (_error) {
      // Leave profiles empty. The visible status/log already tells the user what failed.
    }
  } else {
    if (!state.selectedProfile || !state.profiles.some(p => (p.school || p.row_text) === state.selectedProfile)) {
      const first = state.profiles[0];
      state.selectedProfile = first.school || first.row_text || 'Profile 1';
    }
    const selected = state.profiles.find((p, i) => profileLabel(p, i) === state.selectedProfile);
    state.activeDirectProfile = Boolean(selected?.direct || (state.selectedProfile.startsWith('Current active INNA session') || state.selectedProfile.startsWith('Current INNA session')));
  }

  render();
}

async function refreshCatalog() {
  if (!state.selectedProfile && state.profiles.length > 1) {
    appendLog('Choose an INNA profile/school before refreshing semesters.');
    return;
  }
  if (state.commandBusy) {
    appendLog('Another INNA operation is still running. Wait a moment.');
    return;
  }

  appendLog(`Refreshing semesters for: ${state.selectedProfile || 'current INNA session'}`);
  try {
    const data = await sidecarJson('catalog');
    state.terms = data.terms || [];
    state.selectedTerms = new Set(
      state.terms
        .filter(t => (t.courses || []).some(c => c.viewable))
        .map(t => String(t.term_code || ''))
        .filter(Boolean)
    );

    if (state.terms.length === 0) {
      appendLog('No semesters returned. The catalog JSON was readable, but INNA returned no terms.');
    } else {
      appendLog(`Loaded ${state.terms.length} semester(s).`);
    }
  } catch (error) {
    appendLog(`Refresh semesters failed: ${String(error)}`);
    appendLog('Run diagnostics and send the log tail if this keeps happening.');
  }

  render();
}

async function runDiagnostics() {
  appendLog('\n=== INNA GUI diagnostics ===');
  appendLog(`selectedProfile=${state.selectedProfile || '(none)'}`);
  appendLog(`activeDirectProfile=${state.activeDirectProfile}`);
  appendLog(`profiles=${state.profiles.length}`);
  appendLog(`terms=${state.terms.length}`);
  appendLog(`selectedTerms=${selectedTermsCsv() || '(none)'}`);
  appendLog(`ownDataConfirmed=${state.ownDataConfirmed}`);
  appendLog(`canDownload=${canDownload()} (${downloadBlockReason() || 'ok'})`);

  try {
    const status = await sidecarJson('status');
    appendLog('STATUS JSON:');
    appendLog(JSON.stringify(status, null, 2));
  } catch (error) {
    appendLog(`STATUS failed: ${String(error)}`);
  }

  try {
    const profiles = await sidecarJson('profiles');
    appendLog('PROFILES JSON:');
    appendLog(JSON.stringify(profiles, null, 2));
    appendLog('If school detection still fails, send the file in AppData named inna-profile-debug-latest.json.');
  } catch (error) {
    appendLog(`PROFILES failed: ${String(error)}`);
  }
}

async function chooseOutput() {
  const selected = await pickFolder({ directory: true, multiple: false });
  if (typeof selected === 'string') {
    state.output = selected;
    render();
  }
}

function selectedTermsCsv(): string {
  return [...state.selectedTerms].filter(Boolean).join(',');
}

function canDownload(): boolean {
  return Boolean(
    !state.running &&
    state.ownDataConfirmed &&
    state.selectedProfile &&
    state.selectedTerms.size > 0
  );
}

function downloadBlockReason(): string {
  if (state.running) return 'Download is already running.';
  if (!state.ownDataConfirmed) return 'Tick the confirmation under Login before downloading.';
  if (!state.selectedProfile) return 'Choose a school/profile first.';
  if (state.selectedTerms.size === 0) return 'Refresh semesters and select at least one semester.';
  return '';
}

function readinessText(): string {
  if (!state.selectedProfile) return 'Choose a school/profile.';
  if (state.terms.length === 0) return 'Refresh semesters for the selected school.';
  if (state.selectedTerms.size === 0) return 'Select at least one semester.';
  if (!state.ownDataConfirmed) return 'Tick the confirmation checkbox.';
  return 'Ready to download.';
}

function updateDownloadReadinessUi() {
  const download = document.querySelector<HTMLButtonElement>('#download');
  if (download) {
    download.disabled = !canDownload();
    download.textContent = state.running ? 'Running…' : 'Download selected semesters';
  }

  const reason = document.querySelector<HTMLElement>('#downloadBlockReason');
  if (reason) {
    const text = downloadBlockReason();
    reason.textContent = text;
    reason.style.display = text ? 'block' : 'none';
  }

  const loginStatusBox = document.querySelector<HTMLDivElement>('#loginStatus');
  const loginStatus = document.querySelector<HTMLElement>('#loginStatus .status-text');
  if (loginStatusBox && loginStatus && (state.selectedProfile || state.profiles.length > 0)) {
    loginStatusBox.className = `status ${canDownload() ? 'green' : 'yellow'}`;
    loginStatus.textContent = canDownload() ? 'INNA ready · Ready to download' : `INNA ready · ${readinessText()}`;
  }
}

function setLoginStatusUi(kind: 'red' | 'yellow' | 'green', text: string) {
  const el = document.querySelector<HTMLDivElement>('#loginStatus');
  if (!el) return;
  el.className = `status ${kind}`;
  const statusTextEl = el.querySelector('.status-text');
  if (statusTextEl) statusTextEl.textContent = text;
}

function stopLoginWatcher() {
  if (state.loginWatcherTimer) {
    clearInterval(state.loginWatcherTimer as any);
    state.loginWatcherTimer = 0 as any;
  }
}

function shouldAutoFetchProfilesFromStatus(status: Status): boolean {
  if (state.loginAutoProfileFetched) return false;
  if (state.selectedProfile || state.profiles.length > 0) return false;
  return Boolean(status.logged_in || status.profile_selector_ready);
}

async function autoFetchProfilesAfterLogin(status: Status) {
  if (!shouldAutoFetchProfilesFromStatus(status)) return;

  state.loginAutoProfileFetched = true;

  if (status.profile_selector_ready && (status.profiles || []).length > 0) {
    applyProfiles(status.profiles || [], 'automatic login check');
    setLoginStatusUi('yellow', 'Login detected · choose school/profile');
    stopLoginWatcher();
    return;
  }

  if (status.logged_in && !status.profile_selector_ready) {
    synthesizeProfileFromStatus(status);
    setLoginStatusUi('yellow', `Login detected · ${state.selectedProfile || 'current school'}`);
    stopLoginWatcher();
    return;
  }

  try {
    await refreshProfiles();
    if (state.profiles.length > 0 || state.selectedProfile) {
      setLoginStatusUi('yellow', 'Login detected · school list ready');
      stopLoginWatcher();
    }
  } catch (error) {
    appendLog(`Automatic school-list fetch failed: ${String(error)}`);
  }
}

async function checkStatusOnce(reason = 'manual') {
  if (state.commandBusy || state.running) {
    if (reason === 'manual') appendLog('Another INNA operation is running. Login check skipped.');
    return null;
  }

  try {
    const status = await sidecarJson('status') as Status;

    if (!state.selectedProfile && state.profiles.length === 0 && status.profile_selector_ready && (status.profiles || []).length > 0) {
      state.profiles = status.profiles || [];
      appendLog(`Detected ${state.profiles.length} school/profile option(s). Choose one from the dropdown.`);
      render();
    }

    if (!state.selectedProfile && state.profiles.length === 0 && status.logged_in && !status.profile_selector_ready) {
      synthesizeProfileFromStatus(status);
    }

    setLoginStatusUi(statusClass(status) as any, statusText(status));
    await autoFetchProfilesAfterLogin(status);
    return status;
  } catch (error) {
    if (reason === 'manual') {
      setLoginStatusUi('red', `${String(error)}`);
    } else {
      setLoginStatusUi('yellow', 'Waiting for login / Chrome connection…');
    }
    return null;
  }
}

function startLoginWatcher(maxSeconds = 150) {
  stopLoginWatcher();
  state.loginAutoProfileFetched = false;
  state.loginWatcherStartedAt = Date.now();
  setLoginStatusUi('yellow', 'Waiting for INNA login…');

  const tick = async () => {
    const elapsed = (Date.now() - state.loginWatcherStartedAt) / 1000;
    if (elapsed > maxSeconds) {
      stopLoginWatcher();
      setLoginStatusUi('yellow', 'Login check timed out. Click Check now after finishing login.');
      return;
    }

    const status = await checkStatusOnce('watcher');
    if (!status) return;

    if (status.logged_in || status.profile_selector_ready) {
      await autoFetchProfilesAfterLogin(status);
      if (state.profiles.length > 0 || state.selectedProfile) {
        stopLoginWatcher();
      }
    }
  };

  setTimeout(tick, 900);
  state.loginWatcherTimer = setInterval(tick, 3000);
}

async function startDownload() {
  stopLoginWatcher();
  if (!canDownload()) {
    appendLog(downloadBlockReason());
    render();
    return;
  }
  if (state.running) return;
  state.running = true;
  state.stopRequested = false;
  state.log = '';
  state.sidecarLogFile = '';
  state.sidecarEventsFile = '';
  state.sidecarLastLogLength = 0;
  state.sidecarLastEventsLength = 0;
  resetProgressState();
  await ensureSidecarLogFile();
  await ensureSidecarEventsFile();
  await startLogPolling();
  await startEventPolling();
  render();

  appendLog(`Archive output folder: ${state.output}`);
  const terms = selectedTermsCsv();
  const args = [
    'download',
    ...baseArgs(),
    ...privacyArgs(),
  ];

  if (state.selectedProfile && !state.activeDirectProfile) {
    args.push('--school', state.selectedProfile, '--force-school-select');
  }

  if (terms) args.push('--terms', terms);
  else args.push('--all');

  const logFile = await ensureSidecarLogFile();
  const eventsFile = await ensureSidecarEventsFile();
  const fullArgs = [...args, '--log-file', logFile, '--events-file', eventsFile];
  appendLog(`$ inna_downloader_cli ${fullArgs.join(' ')}\n`);

  try {
    const code = await invoke<number>('run_downloader', { args: fullArgs });
    await importSidecarLog('final sidecar log');
    await importSidecarEvents('final progress events');

    if (code === -2 || state.stopRequested) {
      appendLog(`\nDownload stopped by user.\n`);
      state.progress.step = 'Stopped by user.';
      setProgressDom();
    } else {
      appendLog(`\nProcess exited with code ${code}.\n`);
      appendLog(`Expected archive files: ${state.output}\\archive_index.json and ${state.output}\\viewer.html`);
    }
  } catch (error) {
    appendLog(`\nProcess error: ${String(error)}\n`);
    await appendSidecarLogTail('download error');
  } finally {
    stopLogPolling();
    stopEventPolling();
    state.running = false;
    render();
  }
}

async function stopDownload() {
  if (!state.running) {
    appendLog('No download is currently running.');
    return;
  }

  state.stopRequested = true;
  appendLog('Stopping downloader sidecar…');

  try {
    const stopped = await invoke<boolean>('stop_downloader');
    if (stopped) {
      appendLog('Stop signal sent. Waiting for sidecar to exit…');
      state.progress.step = 'Stopping…';
      setProgressDom();
    } else {
      appendLog('No active sidecar process was found to stop.');
    }
  } catch (error) {
    appendLog(`Stop failed: ${String(error)}`);
  }
}

async function openOutput() {
  await invoke('open_path', { path: state.output });
}

async function openViewer() {
  // Works best after an archive run copied viewer.html into the output folder.
  const viewerPath = `${state.output}\\viewer.html`;
  await invoke('open_path', { path: viewerPath });
}

function setBar(id: string, pctValue: number) {
  const clamped = Math.max(0, Math.min(100, pctValue));
  if (id === 'courseBar') state.progress.coursePct = clamped;
  if (id === 'materialsBar') state.progress.materialsPct = clamped;
  if (id === 'assignmentsBar') state.progress.assignmentsPct = clamped;

  const el = document.querySelector<HTMLDivElement>(`#${id} > div`);
  if (el) el.style.width = `${clamped}%`;
}

function updateProgressFromLine(_line: string) {
  // Deprecated: progress now comes from JSONL events, not human log text.
}

function render() {
  app.innerHTML = `
    <div class="app">
      <aside class="sidebar">
        <div class="brand">
          <div class="logo"></div>
          <div>
            <h1>INNAGUNN</h1>
            <div class="small">INNA downloader</div>
          </div>
        </div>

        <div id="loginStatus" class="status yellow">
          <div class="dot"></div>
          <div class="status-text">Click Start Chrome / login to begin automatic login check</div>
        </div>

        <div class="card">
          <h3>1. Login</h3>
          <p class="small">Opnar Chrome og þú þarft að skrá þig inn á google. (EKKI GERA NEITT UMFRAM ÞAÐ SVO, HALTU GLUGGANUM OPNUM Á MEÐAN NIÐURHAL STENDUR Á)</p>
          <div class="row wrap">
            <button id="startChrome">Start Chrome / login</button>
            <button class="secondary" id="checkStatus">Check now</button>
            <button class="secondary" id="runDiagnostics">Run diagnostics</button>
          </div>

          <label class="confirm-box">
            <input id="ownDataConfirmed" type="checkbox" ${state.ownDataConfirmed ? 'checked' : ''} />
            <span>
              <strong>Staðfesting <button id="disclaimerHelp" class="tiny-help" type="button" title="Sýna nánari texta">?</button></strong><br>
              <span class="small">Þetta eru mín gögn sem ég ætla að niðurhala. Ég hef lesið, skilið, og ætla að nota forritið eins og skilmálar segja til.</span>
            </span>
          </label>

        </div>

        <div class="card">
          <h3>2. School / INNA profile</h3>
          <p class="small">Ýttu á endurnýja lista og veldu skóla, ef það er bara einn skóli þá veldu hann.</p>
          <button class="secondary" id="refreshProfiles">Endurnýja skólalista</button>

          <div class="field" style="margin-top: 12px;">
            <label class="small">Veldu skóla</label>
            <select id="profileSelect" ${state.profiles.length ? '' : 'disabled'}>
              ${state.profiles.length ? `<option value="" ${state.selectedProfile ? '' : 'selected'} disabled>Choose school/profile…</option>` + state.profiles.map((p, i) => {
                const label = profileLabel(p, i);
                return `<option value="${escapeHtml(label)}" ${state.selectedProfile === label ? 'selected' : ''}>${escapeHtml(label)}</option>`;
              }).join('') : '<option>No profiles loaded yet</option>'}
            </select>
          </div>

          <div class="small" style="margin-top: 8px;">
            ${(() => {
              const selected = state.profiles.find((p, i) => profileLabel(p, i) === state.selectedProfile);
              if (!selected) return state.profiles.length ? 'Choose a school/profile from the dropdown.' : 'Click “Refresh school list” after logging in.';
              return `Selected: <strong>${escapeHtml(profileLabel(selected))}</strong><br>${escapeHtml([selected.name, selected.status].filter(Boolean).join(' · '))}`;
            })()}
          </div>
        </div>

        <div class="card">
          <h3>3. Output</h3>
          <div class="field">
            <label class="small">Archive folder</label>
            <input id="output" value="${escapeHtml(state.output)}" />
          </div>
          <button class="secondary" id="chooseOutput">Veldu staðsetningu</button>
        </div>
      </aside>

      <main class="main">
        <div class="notice">
          This is an unofficial local tool. Use only with your own INNA account. Exported archives may contain grades,
          teacher feedback, personal data, and copyrighted course materials. Keep them private.
        </div>

        <div class="grid" style="margin-top: 16px;">
          <section class="card semester-card">
            <h2>Semesters</h2>
            <div class="row wrap" style="margin-bottom: 10px;">
              <button class="secondary" id="refreshCatalog">Refresh semesters</button>
              <button class="secondary" id="selectAllTerms">Select all</button>
              <button class="secondary" id="selectNoTerms">Select none</button>
            </div>
            <div class="term-list">
              ${state.terms.map(t => {
                const code = String(t.term_code || '');
                const total = (t.courses || []).length;
                const viewable = (t.courses || []).filter(c => c.viewable).length;
                return `<label class="term">
                  <input type="checkbox" data-term="${escapeHtml(code)}" ${state.selectedTerms.has(code) ? 'checked' : ''} />
                  <span><strong>${escapeHtml(code)}</strong><br><span class="small">${escapeHtml(t.term_name || '')} · ${viewable}/${total} viewable courses</span></span>
                </label>`;
              }).join('') || '<div class="small">No semesters loaded yet.</div>'}
            </div>
          </section>

          <section class="card">
            <h2>Archive settings</h2>
            <label class="term"><input id="privacyMode" type="checkbox" ${state.privacyMode ? 'checked' : ''}/> <span><strong>Privacy mode</strong><br><span class="small">Skips raw endpoint JSON. Hópalisti is controlled separately below.</span></span></label>
            <label class="term"><input id="includeClassmates" type="checkbox" ${state.includeClassmates ? 'checked' : ''}/> <span><strong>Include Hópalisti / names and emails</strong><br><span class="small">Overrides the privacy preset for this one item. Keep the archive private.</span></span></label>
            <label class="term"><input id="renderPdf" type="checkbox" ${state.renderPdf ? 'checked' : ''}/> <span><strong>Render PDF snapshots</strong><br><span class="small">Slower. HTML is the default archive view.</span></span></label>
            <label class="term"><input id="includeMedia" type="checkbox" ${state.includeMedia ? 'checked' : ''}/> <span><strong>Attempt video/audio downloads</strong><br><span class="small">Stalled media times out after 5 seconds without bytes.</span></span></label>
            <label class="term"><input id="debugApi" type="checkbox" ${state.debugApi ? 'checked' : ''}/> <span><strong>Debug: log INNA API calls</strong><br><span class="small">Shows API GET/OK lines like the original Python run. Useful while testing assignments.</span></span></label>
            <label class="term"><input id="assignmentDebug" type="checkbox" ${state.assignmentDebug ? 'checked' : ''}/> <span><strong>Debug: assignment endpoint summaries</strong><br><span class="small">For each assignment, logs whether duration/info/student/submitted attachments/details were fetched.</span></span></label>
            <label class="term"><input id="debugDownloads" type="checkbox" ${state.debugDownloads ? 'checked' : ''}/> <span><strong>Debug: verbose file downloads</strong><br><span class="small">Noisier. Shows more detail about file download starts and byte progress.</span></span></label>
          </section>
        </div>

        <section class="card">
          <h2>Progress</h2>
          <p id="progressStep" class="small" style="margin-top:0;">${escapeHtml(state.progress.step)}</p>
          <div class="field">
            <label class="small">Course</label>
            <div id="courseBar" class="progress"><div style="width:${state.progress.coursePct}%"></div></div>
            <div id="courseProgressText" class="small">${escapeHtml(state.progress.courseText)}</div>
          </div>
          <div class="field">
            <label class="small">Efni / materials</label>
            <div id="materialsBar" class="progress"><div style="width:${state.progress.materialsPct}%"></div></div>
            <div id="materialsProgressText" class="small">${escapeHtml(state.progress.materialsText)}</div>
          </div>
          <div class="field">
            <label class="small">Assignments / próf</label>
            <div id="assignmentsBar" class="progress"><div style="width:${state.progress.assignmentsPct}%"></div></div>
            <div id="assignmentsProgressText" class="small">${escapeHtml(state.progress.assignmentsText)}</div>
          </div>
          <div class="row wrap">
            <button id="download" ${!canDownload() ? 'disabled' : ''}>${state.running ? 'Running…' : 'Download selected semesters'}</button>
            <button class="danger" id="stop" ${state.running ? '' : 'disabled'}>Stop</button>
            <button class="secondary" id="openOutput">Open output folder</button>
            <button class="secondary" id="openViewer">Open viewer</button>
          </div>
          <p id="downloadBlockReason" class="small" style="color:#9a6400;margin-top:10px;${downloadBlockReason() ? '' : 'display:none;'}">${escapeHtml(downloadBlockReason())}</p>
        </section>

        <section class="card">
          <h2>Log</h2>
          <textarea id="log" readonly>${escapeHtml(state.log)}</textarea>
        </section>
      </main>

      <div id="disclaimerBackdrop" class="modal-backdrop ${state.showDisclaimerDetails ? '' : 'hidden'}" ${state.showDisclaimerDetails ? '' : 'hidden'}></div>
      <section id="disclaimerModal" class="disclaimer-modal ${state.showDisclaimerDetails ? '' : 'hidden'}" ${state.showDisclaimerDetails ? '' : 'hidden'} role="dialog" aria-modal="true" aria-labelledby="disclaimerModalTitle">
        <div class="disclaimer-modal-header">
          <h3 id="disclaimerModalTitle">skilmálar fyrir notkun</h3>
          <button id="disclaimerClose" class="secondary modal-close" type="button" aria-label="Loka">×</button>
        </div>
        <div class="disclaimer-modal-body">
          ${escapeHtml(DETAILED_DISCLAIMER).replace(/\n/g, '<br>')}
        </div>
        <p class="small modal-source-note">Með því að ýta á tjékkboxið samþykki ég skilmálana.</p>
      </section>
    </div>
  `;

  document.querySelector('#startChrome')?.addEventListener('click', startChrome);
  document.querySelector('#checkStatus')?.addEventListener('click', checkStatus);
  document.querySelector('#runDiagnostics')?.addEventListener('click', runDiagnostics);
  document.querySelector('#refreshProfiles')?.addEventListener('click', refreshProfiles);
  document.querySelector('#refreshCatalog')?.addEventListener('click', refreshCatalog);
  document.querySelector('#chooseOutput')?.addEventListener('click', chooseOutput);
  document.querySelector('#download')?.addEventListener('click', startDownload);
  document.querySelector('#stop')?.addEventListener('click', stopDownload);
  document.querySelector('#openOutput')?.addEventListener('click', openOutput);
  document.querySelector('#openViewer')?.addEventListener('click', openViewer);
  document.querySelector('#selectAllTerms')?.addEventListener('click', () => {
    state.selectedTerms = new Set(state.terms.map(t => String(t.term_code || '')).filter(Boolean));
    render();
  });
  document.querySelector('#selectNoTerms')?.addEventListener('click', () => {
    state.selectedTerms.clear();
    render();
  });

  document.querySelector<HTMLInputElement>('#output')?.addEventListener('change', event => {
    state.output = (event.target as HTMLInputElement).value;
  });
  document.querySelector<HTMLInputElement>('#privacyMode')?.addEventListener('change', event => {
    state.privacyMode = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#includeClassmates')?.addEventListener('change', event => {
    state.includeClassmates = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#renderPdf')?.addEventListener('change', event => {
    state.renderPdf = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#includeMedia')?.addEventListener('change', event => {
    state.includeMedia = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#debugApi')?.addEventListener('change', event => {
    state.debugApi = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#assignmentDebug')?.addEventListener('change', event => {
    state.assignmentDebug = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#debugDownloads')?.addEventListener('change', event => {
    state.debugDownloads = (event.target as HTMLInputElement).checked;
  });
  document.querySelector<HTMLInputElement>('#ownDataConfirmed')?.addEventListener('change', event => {
    state.ownDataConfirmed = (event.target as HTMLInputElement).checked;
    updateDownloadReadinessUi();
  });
  document.querySelector<HTMLButtonElement>('#disclaimerHelp')?.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    openDisclaimerModal();
  });

  document.querySelector<HTMLSelectElement>('#profileSelect')?.addEventListener('change', event => {
    state.selectedProfile = (event.target as HTMLSelectElement).value;
    const selected = state.profiles.find((p, i) => profileLabel(p, i) === state.selectedProfile);
    state.activeDirectProfile = Boolean(selected?.direct || (state.selectedProfile.startsWith('Current active INNA session') || state.selectedProfile.startsWith('Current INNA session')));
    state.terms = [];
    state.selectedTerms.clear();
    render();
  });

  document.querySelectorAll<HTMLInputElement>('input[data-term]').forEach(el => {
    el.addEventListener('change', () => {
      const term = el.dataset.term || '';
      if (el.checked) state.selectedTerms.add(term);
      else state.selectedTerms.delete(term);
      updateDownloadReadinessUi();
    });
  });

  const log = document.querySelector<HTMLTextAreaElement>('#log');
  if (log) log.scrollTop = log.scrollHeight;
}

function escapeHtml(value: string): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

render();
document.addEventListener('keydown', event => {
  // disclaimer-escape-listener
  if (event.key === 'Escape' && state.showDisclaimerDetails) {
    closeDisclaimerModal();
  }
});

document.addEventListener('click', event => {
  // disclaimer-global-click-listener
  const target = event.target as HTMLElement | null;
  if (!target) return;

  if (target.closest('#disclaimerClose')) {
    event.preventDefault();
    event.stopPropagation();
    closeDisclaimerModal();
    return;
  }

  if (target.id === 'disclaimerBackdrop') {
    event.preventDefault();
    event.stopPropagation();
    closeDisclaimerModal();
  }
});
initializeDefaultOutput();
// Very gentle background freshness check after login has already been detected.
// This does not drive the login flow; startLoginWatcher() does that after Start Chrome.
setInterval(() => {
  if (!state.running && !state.commandBusy && !state.loginWatcherTimer && (state.selectedProfile || state.profiles.length > 0) && state.terms.length === 0) {
    checkStatusOnce('background');
  }
}, 60000);
