<script lang="ts">
	import { onMount } from 'svelte';
	let token = $state('');
	let templates = $state<any[]>([]);
	let environments = $state<any[]>([]);
	let error = $state('');
	let busy = $state(false);
	let editing = $state<any>(null);
	let configuration = $state('{}');
	let lifecycle = $state('{}');
	const input = 'w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm dark:border-gray-700';
	const button = 'rounded-lg px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50';
	async function api(path: string, method = 'GET', body?: unknown) {
		const response = await fetch(`/api/v1/terminals${path}`, {method, headers: {Authorization: `Bearer ${token}`, ...(body !== undefined ? {'Content-Type': 'application/json'} : {})}, ...(body !== undefined ? {body: JSON.stringify(body)} : {})});
		const data = await response.json();
		if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
		return data;
	}
	async function run(work: () => Promise<void>) {busy = true; error = ''; try {await work();} catch(e) {error = String(e);} finally {busy = false;}}
	async function load() { [templates, environments] = await Promise.all([api('/templates'), api('')]); }
	async function edit(template: any = null) {
		await run(async () => {
			editing = template ?? {id: crypto.randomUUID(), fresh: true, data: {name: '', scope: null}};
			configuration = JSON.stringify(editing.data, null, 2);
			lifecycle = JSON.stringify(template ? (await api(`/templates/${encodeURIComponent(template.id)}/lifecycle`)).data : {}, null, 2);
		});
	}
	async function save(event: SubmitEvent) {
		event.preventDefault();
		await run(async () => {
			const form = new FormData(event.currentTarget as HTMLFormElement);
			const data = {...JSON.parse(configuration), name: form.get('name'), scope: form.get('scope') || null};
			const schedule = JSON.parse(lifecycle);
			if (editing.fresh) {await api('/templates', 'POST', {id: editing.id, data}); editing.fresh = false;}
			else await api(`/templates/${encodeURIComponent(editing.id)}`, 'PATCH', data);
			await api(`/templates/${encodeURIComponent(editing.id)}/lifecycle`, 'PUT', schedule);
			editing = null; await load();
		});
	}
	function context(row: any) {const [scope, ...parts] = row.context_id.split(':'); return {user_id: 'admin', ...(scope === 'user' ? {user_id: parts.join(':')} : scope === 'chat' ? {chat_id: parts.join(':')} : scope === 'model' ? {model_id: parts.join(':')} : {})};}
	async function action(row: any, action: string) {
		if (!confirm(action === 'restart' ? 'Stop running work and retain files?' : 'Remove files and running work for everyone using this environment?')) return;
		await run(async () => {await api(`/templates/${encodeURIComponent(row.template_id)}/${action === 'delete' ? 'instance' : action}`, action === 'delete' ? 'DELETE' : 'POST', context(row)); await load();});
	}
	async function remove(template: any) {
		const count = environments.filter(row => row.template_id === template.id).length;
		if (!confirm(`Delete ${template.data.name ?? template.id} and ${count} environments, including their files?`)) return;
		await run(async () => {await api(`/templates/${encodeURIComponent(template.id)}`, 'DELETE'); await load();});
	}
	onMount(() => {token = sessionStorage.getItem('terminals-key') ?? ''; if (token) void run(load);});
</script>
<svelte:head><title>Terminals</title></svelte:head>
<main class="mx-auto max-w-5xl space-y-6 p-6 text-gray-900 dark:text-gray-100">
	<header class="flex items-center justify-between"><h1 class="text-xl font-semibold">Terminals <span class="text-sm font-normal text-gray-500">0.4.0</span></h1><button class={button} disabled={busy} onclick={() => edit()}>New terminal</button></header>
	<form class="flex gap-2" onsubmit={(event) => {event.preventDefault(); sessionStorage.setItem('terminals-key', token); void run(load);}}><input class={input} type="password" bind:value={token} placeholder="Server key" aria-label="Server key" /><button class={button} disabled={busy}>Connect</button></form>
	{#if error}<p role="alert" class="text-sm text-red-500">{error}</p>{/if}
	{#if editing}<form class="space-y-3 rounded-xl border border-gray-200 p-4 dark:border-gray-800" onsubmit={save}>
		<h2 class="font-medium">{editing.fresh ? 'New terminal' : 'Configure terminal'}</h2>
		<label class="block text-sm">Name<input class={input} name="name" value={editing.data.name ?? editing.id} required /></label>
		<label class="block text-sm">Scope<select class={input} name="scope" value={editing.data.scope ?? ''}><option value="">None</option><option value="user">User</option><option value="chat">Chat</option><option value="model">Model</option></select></label>
		<label class="block text-sm">Instance configuration<textarea class={`${input} font-mono`} rows="8" bind:value={configuration} /></label>
		<label class="block text-sm">Lifecycle schedule<textarea class={`${input} font-mono`} rows="4" bind:value={lifecycle} /></label>
		<div class="flex justify-end"><button type="button" class={button} onclick={() => editing = null}>Cancel</button><button class={button} disabled={busy}>Save</button></div>
	</form>{/if}
	<section><h2 class="mb-3 font-medium">Templates</h2><div class="space-y-2">{#each templates as template (template.id)}<div class="flex items-center justify-between rounded-xl border border-gray-200 p-3 dark:border-gray-800"><div><p class="text-sm">{template.data.name ?? template.id}</p><p class="text-xs text-gray-500">{template.data.scope ?? 'None'} · {template.data.image ?? 'Server default'}</p></div><div><button class={button} disabled={busy} onclick={() => edit(template)}>Configure</button><button class={button} disabled={busy} onclick={() => remove(template)}>Delete terminal</button></div></div>{:else}<p class="text-sm text-gray-500">No templates yet.</p>{/each}</div></section>
	<section><h2 class="mb-3 font-medium">Environments</h2><div class="space-y-2">{#each environments as row (row.id)}<div class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-200 p-3 dark:border-gray-800"><div><p class="text-sm">{templates.find(t => t.id === row.template_id)?.data.name ?? row.template_id}</p><p class="text-xs text-gray-500">{row.context_id} · {row.status}</p></div><div><button class={button} disabled={busy} onclick={() => action(row, 'restart')}>Restart</button><button class={button} disabled={busy} onclick={() => action(row, 'reset')}>Reset</button><button class={button} disabled={busy} onclick={() => action(row, 'delete')}>Delete environment</button></div></div>{:else}<p class="text-sm text-gray-500">No environments created.</p>{/each}</div></section>
</main>
