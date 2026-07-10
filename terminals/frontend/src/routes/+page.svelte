<script lang="ts">
	import { onMount } from 'svelte';

	type AdminStatus = {
		status: boolean;
		backend: string;
		active_terminals: number;
		policy_count: number;
	};

	type TerminalRow = {
		user_id: string;
		policy_id: string;
		status: string;
		instance_id: string;
		instance_name: string;
		last_active_at: string | null;
		idle_timeout_minutes: number;
	};

	type PolicyRow = {
		id: string;
		data: Record<string, any>;
		lifecycle?: Record<string, any>;
		created_at?: string | null;
		updated_at?: string | null;
	};

	type EditingPolicy = {
		id: string;
		existing: boolean;
		data: Record<string, any>;
		lifecycle: Record<string, any>;
	};

	let token = $state('');
	let status = $state<AdminStatus | null>(null);
	let terminals = $state<TerminalRow[]>([]);
	let policies = $state<PolicyRow[]>([]);
	let error = $state('');
	let notice = $state('');
	let loading = $state(false);
	let editingPolicy = $state<EditingPolicy | null>(null);
	const envPlaceholder = '{"OPEN_TERMINAL_ALLOWED_DOMAINS":"github.com"}';
	const podSecurityPlaceholder = '{"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}}';
	const containerSecurityPlaceholder =
		'{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true}';

	onMount(() => {
		token = localStorage.getItem('terminals.adminToken') || '';
		void loadAll();
	});

	function headers(json = false): HeadersInit {
		return {
			...(token ? { Authorization: `Bearer ${token}` } : {}),
			...(json ? { 'Content-Type': 'application/json' } : {})
		};
	}

	async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
		const res = await fetch(path, {
			...init,
			headers: {
				...headers(Boolean(init.body)),
				...(init.headers || {})
			}
		});
		if (!res.ok) {
			let message = `${res.status} ${res.statusText}`;
			try {
				const body = await res.json();
				message = body.detail || body.error || message;
			} catch {}
			throw new Error(message);
		}
		return (await res.json()) as T;
	}

	async function loadAll() {
		loading = true;
		error = '';
		try {
			const [nextStatus, nextTerminals, nextPolicies] = await Promise.all([
				api<AdminStatus>('/api/v1/status'),
				api<TerminalRow[]>('/api/v1/terminals'),
				api<PolicyRow[]>('/api/v1/policies')
			]);
			const policiesWithLifecycle = await Promise.all(
				nextPolicies.map(async (policy) => {
					const lifecycle = await api<{ data?: Record<string, any> }>(
						`/api/v1/policies/${encodeURIComponent(policy.id)}/lifecycle`
					);
					return { ...policy, lifecycle: lifecycle.data || {} };
				})
			);
			status = nextStatus;
			terminals = nextTerminals;
			policies = policiesWithLifecycle;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load';
		} finally {
			loading = false;
		}
	}

	function saveToken(value: string) {
		token = value;
		localStorage.setItem('terminals.adminToken', value);
	}

	function lastActive(value: string | null) {
		if (!value) return 'never';
		const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
		if (seconds < 60) return `${seconds}s ago`;
		if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
		if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
		return new Date(value).toLocaleDateString();
	}

	function policyValue(policy: PolicyRow, key: string, fallback = '-') {
		return policy.data?.[key] ?? fallback;
	}

	function resetSchedule(policy: PolicyRow) {
		return policy.lifecycle?.reset?.schedule ?? '-';
	}

	function restrictedLabel(policy: PolicyRow) {
		return policy.data?.restricted ? 'restricted' : 'standard';
	}

	function statusClass(value: string) {
		if (value === 'running') return 'bg-emerald-600';
		if (value === 'stopped' || value === 'missing') return 'bg-gray-400';
		return 'bg-amber-500';
	}

	async function stopTerminal(row: TerminalRow) {
		error = '';
		notice = '';
		try {
			await api('/api/v1/terminals/stop', {
				method: 'POST',
				body: JSON.stringify({ user_id: row.user_id, policy_id: row.policy_id })
			});
			await loadAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to stop terminal';
		}
	}

	async function rolloutPolicy(policy: PolicyRow) {
		error = '';
		notice = '';
		try {
			const result = await api<{ refreshed: number; skipped_active: number }>('/api/v1/terminals/refresh', {
				method: 'POST',
				body: JSON.stringify({ policy_id: policy.id, only_idle: true })
			});
			notice = `Rolled out to ${result.refreshed} idle terminal${result.refreshed === 1 ? '' : 's'}${result.skipped_active ? `; ${result.skipped_active} active skipped` : ''}.`;
			await loadAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to roll out policy';
		}
	}

	function openPolicy(policy: PolicyRow | null = null) {
		notice = '';
		editingPolicy = policy
			? {
					id: policy.id,
					existing: true,
					data: { ...(policy.data || {}) },
					lifecycle: { ...(policy.lifecycle || {}) }
				}
			: { id: '', existing: false, data: {}, lifecycle: {} };
	}

	async function deletePolicy(policy: PolicyRow) {
		if (!confirm(`Delete policy "${policy.id}"?`)) return;
		error = '';
		notice = '';
		try {
			await api(`/api/v1/policies/${encodeURIComponent(policy.id)}`, { method: 'DELETE' });
			await loadAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to delete policy';
		}
	}

	function readPolicyForm(form: HTMLFormElement) {
		const data = new FormData(form);
		const id = String(data.get('id') || '').trim();
		const body: Record<string, any> = {};
		for (const key of ['image', 'cpu_limit', 'memory_limit', 'storage', 'storage_mode']) {
			const value = String(data.get(key) || '').trim();
			if (value) body[key] = value;
		}
		const idle = Number(data.get('idle_timeout_minutes'));
		if (Number.isFinite(idle) && idle > 0) body.idle_timeout_minutes = idle;
		if (data.get('restricted') === 'on') body.restricted = true;
		const env = String(data.get('env') || '').trim();
		if (env) body.env = JSON.parse(env);
		const podSecurityContext = String(data.get('pod_security_context') || '').trim();
		if (podSecurityContext) body.pod_security_context = JSON.parse(podSecurityContext);
		const containerSecurityContext = String(data.get('container_security_context') || '').trim();
		if (containerSecurityContext) {
			body.container_security_context = JSON.parse(containerSecurityContext);
		}
		return { id, body };
	}

	function readLifecycleForm(form: HTMLFormElement) {
		const data = new FormData(form);
		const schedule = String(data.get('reset_schedule') || '').trim();
		const timezone = String(data.get('reset_timezone') || '').trim();
		if (!schedule) return {};
		return {
			reset: {
				schedule,
				...(timezone ? { timezone } : {})
			}
		};
	}

	async function savePolicy(event: SubmitEvent) {
		event.preventDefault();
		error = '';
		notice = '';
		try {
			const { id, body } = readPolicyForm(event.currentTarget as HTMLFormElement);
			const lifecycle = readLifecycleForm(event.currentTarget as HTMLFormElement);
			if (!id) throw new Error('Policy ID is required');
			await api(`/api/v1/policies/${encodeURIComponent(id)}`, {
				method: 'PUT',
				body: JSON.stringify(body)
			});
			await api(`/api/v1/policies/${encodeURIComponent(id)}/lifecycle`, {
				method: 'PUT',
				body: JSON.stringify(lifecycle)
			});
			editingPolicy = null;
			await loadAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to save policy';
		}
	}

</script>

<svelte:head>
	<title>Terminals</title>
</svelte:head>




<div class="app-theme min-h-dvh bg-white font-sans antialiased text-gray-700">
	<div class="grid min-h-dvh grid-cols-1 md:grid-cols-[16.5rem_minmax(0,1fr)]">
		<aside class="hidden border-r border-gray-200 bg-gray-50/50 px-1.5 py-1.5 md:block">
			<div class="mb-1 flex h-7 items-center gap-1.5 px-2 text-xs font-medium text-gray-900">
				<span class="flex h-3.5 w-3.5 items-center justify-center rounded-[3px] bg-gray-900 text-[8px] text-white">▣</span>
				<span>Terminals</span>
			</div>
			<div class="mb-1 h-px bg-gray-200"></div>
			<nav class="space-y-px text-xs">
				<a class="block h-7 rounded-md bg-gray-200/70 px-2 leading-7 font-medium text-gray-900" href="#terminals">Terminals</a>
				<a class="block h-7 rounded-md px-2 leading-7 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700" href="#policies">Policies</a>
			</nav>
		</aside>

		<main class="min-w-0">
			<div class="flex h-9 items-center px-3">
				<h1 class="px-0.5 text-xs font-medium text-gray-700">Terminals</h1>
				<div class="ml-4 flex items-center gap-3 text-[11px] text-gray-500">
					<span>{status?.backend ?? '-'}</span>
					<span>{status?.active_terminals ?? terminals.length} active</span>
					<span>{status?.policy_count ?? policies.length} policies</span>
				</div>
				<div class="ml-auto flex min-w-0 items-center gap-1.5">
					<input
						class="h-6 w-40 min-w-0 border-0 bg-transparent px-1.5 text-xs text-gray-700 outline-none placeholder:text-gray-400 focus:bg-gray-100 sm:w-52"
						type="password"
						placeholder="Admin key"
						value={token}
						oninput={(event) => saveToken(event.currentTarget.value)}
					/>
					<button
						class="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700"
						type="button"
						aria-label="Refresh"
						title="Refresh"
						onclick={() => loadAll()}
						disabled={loading}
					>
						<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
							<path d="M21 12a9 9 0 0 1-15 6.7" />
							<path d="M3 12a9 9 0 0 1 15-6.7" />
							<path d="M3 19v-5h5" />
							<path d="M21 5v5h-5" />
						</svg>
					</button>
				</div>
			</div>

			<section id="terminals" class="pt-3">
				<div class="flex h-7 items-center justify-between px-3">
					<h2 class="text-xs text-gray-500">Active terminals</h2>
				</div>
				{#if terminals.length === 0}
					<div class="px-3 py-4 text-xs text-gray-400">No active terminals.</div>
				{:else}
					<div class="overflow-x-auto">
						<table class="w-full table-fixed border-collapse text-xs">
							<thead class="text-left text-[11px] font-normal text-gray-500">
								<tr>
									<th class="h-7 w-[30%] px-3 font-normal">User</th>
									<th class="h-7 w-[30%] px-2 font-normal">Policy</th>
									<th class="h-7 px-2 font-normal">Status</th>
									<th class="hidden h-7 px-2 font-normal sm:table-cell">Last active</th>
									<th class="hidden h-7 px-2 font-normal lg:table-cell">Instance</th>
									<th class="h-7 w-8 px-1"></th>
								</tr>
							</thead>
							<tbody>
								{#each terminals as terminal}
									<tr class="text-gray-700 hover:bg-gray-50">
										<td class="h-7 truncate px-3">{terminal.user_id}</td>
										<td class="h-7 truncate px-2">
											{policies.some((policy) => policy.id === terminal.policy_id)
												? terminal.policy_id
												: 'Service defaults'}
										</td>
										<td class="h-7 truncate px-2">
											<span class="inline-flex items-center gap-2">
												<span class={`h-1.5 w-1.5 rounded-full ${statusClass(terminal.status)}`}></span>
												{terminal.status}
											</span>
										</td>
										<td class="hidden h-7 truncate px-2 sm:table-cell">{lastActive(terminal.last_active_at)}</td>
										<td class="hidden h-7 truncate px-2 lg:table-cell">{terminal.instance_name || terminal.instance_id}</td>
										<td class="h-7 px-1 text-right">
											<button
												class="inline-flex h-6 w-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700"
												type="button"
												aria-label="Stop"
												title="Stop"
												onclick={() => stopTerminal(terminal)}
											>
												<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" /></svg>
											</button>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</section>

			<section id="policies" class="mt-5">
				<div class="flex h-7 items-center justify-between px-3">
					<h2 class="text-xs text-gray-500">Policies</h2>
					<button
					class="flex h-6 w-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700"
						type="button"
						aria-label="Add policy"
						title="Add policy"
						onclick={() => openPolicy()}
					>
						<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
					</button>
				</div>
				{#if policies.length === 0}
					<div class="px-3 py-4 text-xs text-gray-400">No policies configured.</div>
				{:else}
					<div class="overflow-x-auto">
						<table class="w-full table-fixed border-collapse text-xs">
							<thead class="text-left text-[11px] font-normal text-gray-500">
								<tr>
									<th class="h-7 w-[30%] px-3 font-normal md:w-[20%]">ID</th>
									<th class="hidden h-7 px-2 font-normal md:table-cell md:w-[35%]">Image</th>
									<th class="h-7 px-2 font-normal">CPU</th>
									<th class="h-7 px-2 font-normal">Memory</th>
									<th class="hidden h-7 px-2 font-normal sm:table-cell">Idle</th>
									<th class="h-7 px-2 font-normal">Mode</th>
									<th class="hidden h-7 px-2 font-normal lg:table-cell">Reset</th>
									<th class="h-7 w-24 px-2"></th>
								</tr>
							</thead>
							<tbody>
								{#each policies as policy}
									<tr class="text-gray-700 hover:bg-gray-50">
										<td class="h-7 truncate px-3">{policy.id}</td>
										<td class="hidden h-7 truncate px-2 md:table-cell">{policyValue(policy, 'image', 'default')}</td>
										<td class="h-7 truncate px-2">{policyValue(policy, 'cpu_limit')}</td>
										<td class="h-7 truncate px-2">{policyValue(policy, 'memory_limit')}</td>
										<td class="hidden h-7 truncate px-2 sm:table-cell">{policyValue(policy, 'idle_timeout_minutes')}</td>
										<td class="h-7 truncate px-2">{restrictedLabel(policy)}</td>
										<td class="hidden h-7 truncate px-2 lg:table-cell">{resetSchedule(policy)}</td>
										<td class="h-7 px-2 text-right">
											<div class="inline-flex items-center gap-0.5 whitespace-nowrap">
											<button class="inline-flex h-6 w-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700" type="button" aria-label="Roll out to idle terminals" title="Roll out to idle terminals" onclick={() => rolloutPolicy(policy)}>
												<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.34-5.66L4 8.67" /><path d="M4 4v4.67h4.67" /></svg>
											</button>
											<button class="inline-flex h-6 w-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700" type="button" aria-label="Edit" title="Edit" onclick={() => openPolicy(policy)}>
												<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" /></svg>
											</button>
											<button class="inline-flex h-6 w-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700" type="button" aria-label="Delete" title="Delete" onclick={() => deletePolicy(policy)}>
												<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6l-1 14H6L5 6" /></svg>
											</button>
											</div>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</section>

			{#if notice}
				<div class="px-3 py-2 text-[11px] text-emerald-700">{notice}</div>
			{/if}
			{#if error}
				<div class="px-3 py-2 text-[11px] text-red-700">{error}</div>
			{/if}
		</main>
	</div>
</div>

{#if editingPolicy}
	<div class="fixed inset-0 z-20 flex items-center justify-center bg-black/50 p-4">
		<div class="app-theme max-h-[calc(100vh-2rem)] w-full max-w-xl overflow-auto rounded-3xl border border-gray-200 bg-white p-5 shadow-2xl dark:border-white/6 dark:bg-gray-950">
			<div class="mb-5 flex items-center justify-between">
				<h2 class="text-sm font-medium text-gray-900 dark:text-white">{editingPolicy.existing ? 'Edit policy' : 'Add policy'}</h2>
				<button class="flex h-6 w-6 items-center justify-center rounded-lg text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-600 dark:hover:bg-white/6 dark:hover:text-gray-300" type="button" aria-label="Close" onclick={() => (editingPolicy = null)}>
					<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
				</button>
			</div>

			<form onsubmit={savePolicy}>
				<div class="mb-3 border-b border-gray-200 pb-2 text-xs text-gray-400 dark:border-white/6 dark:text-gray-600">Policy</div>
				<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
					<label class="block text-xs text-gray-600 dark:text-gray-400">ID<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="id" value={editingPolicy.id} readonly={editingPolicy.existing} /></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400">Image<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="image" value={editingPolicy.data.image || ''} placeholder="ghcr.io/open-webui/open-terminal:latest" /></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400">CPU<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="cpu_limit" value={editingPolicy.data.cpu_limit || ''} placeholder="1" /></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400">Memory<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="memory_limit" value={editingPolicy.data.memory_limit || ''} placeholder="1Gi" /></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400">Storage<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="storage" value={editingPolicy.data.storage || ''} placeholder="5Gi" /></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400">Storage mode<select class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="storage_mode" value={editingPolicy.data.storage_mode || ''}>
						<option value="">default</option>
						<option value="per-user">per-user</option>
						<option value="shared">shared</option>
						<option value="shared-rwo">shared-rwo</option>
					</select></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400">Idle timeout<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="idle_timeout_minutes" type="number" min="0" value={editingPolicy.data.idle_timeout_minutes || ''} placeholder="30" /></label>
					<label class="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-400"><input class="h-4 w-4 rounded border-gray-300" name="restricted" type="checkbox" checked={!!editingPolicy.data.restricted} />Restricted Kubernetes/OpenShift</label>
					<label class="block text-xs text-gray-600 dark:text-gray-400 sm:col-span-2">Env JSON<textarea class="mt-1 min-h-24 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 py-2 font-mono text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="env" placeholder={envPlaceholder}>{editingPolicy.data.env ? JSON.stringify(editingPolicy.data.env, null, 2) : ''}</textarea></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400 sm:col-span-2">Pod security context JSON<textarea class="mt-1 min-h-20 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 py-2 font-mono text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="pod_security_context" placeholder={podSecurityPlaceholder}>{editingPolicy.data.pod_security_context ? JSON.stringify(editingPolicy.data.pod_security_context, null, 2) : ''}</textarea></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400 sm:col-span-2">Container security context JSON<textarea class="mt-1 min-h-20 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 py-2 font-mono text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="container_security_context" placeholder={containerSecurityPlaceholder}>{editingPolicy.data.container_security_context ? JSON.stringify(editingPolicy.data.container_security_context, null, 2) : ''}</textarea></label>
				</div>
				<div class="mb-3 mt-5 border-b border-gray-200 pb-2 text-xs text-gray-400 dark:border-white/6 dark:text-gray-600">Lifecycle</div>
				<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
					<label class="block text-xs text-gray-600 dark:text-gray-400">Reset timezone<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="reset_timezone" value={editingPolicy.lifecycle.reset?.timezone || ''} placeholder="UTC" /></label>
					<label class="block text-xs text-gray-600 dark:text-gray-400 sm:col-span-2">Scheduled reset<input class="mt-1 h-7 w-full rounded-lg border border-gray-200 bg-gray-100 px-2 text-xs text-gray-700 outline-none focus:border-gray-400 dark:border-white/8 dark:bg-white/6 dark:text-gray-300 dark:focus:border-white/20" name="reset_schedule" value={editingPolicy.lifecycle.reset?.schedule || ''} placeholder="@weekly, @monthly, cron, or ISO date" /></label>
				</div>
				<div class="sticky bottom-0 -mx-5 mt-4 flex justify-end gap-3 border-t border-gray-200 bg-white px-5 py-3 dark:border-white/6 dark:bg-gray-950">
					<button class="text-xs text-gray-400 transition-colors hover:text-gray-700 dark:hover:text-gray-300" type="button" onclick={() => (editingPolicy = null)}>Cancel</button>
					<button class="text-xs text-gray-600 transition-colors hover:text-gray-900 dark:text-gray-400 dark:hover:text-white" type="submit">Save</button>
				</div>
			</form>
		</div>
	</div>
{/if}
