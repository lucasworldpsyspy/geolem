// Barra superior comum: usuário logado, navegação e sair. Uso: <header class="barra" data-pagina="carteira"></header>
(async function () {
  const barra = document.querySelector('header.barra');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let eu;
  try {
    const r = await fetch('/api/auth/eu');
    if (r.status === 401) { location.href = '/entrar.html?volta=' + encodeURIComponent(location.pathname); return; }
    if (r.status === 403) { location.href = '/entrar.html?trocar=1'; return; }
    eu = await r.json();
  } catch { return; }
  window.GEOLEM_USUARIO = eu;
  const atual = barra.dataset.pagina;
  const itens = [['analise', '/', 'Análise'], ['carteira', '/carteira.html', 'Carteira'], ['laudos', '/laudos.html', 'Laudos']];
  if (eu.papel === 'admin') itens.push(['equipe', '/equipe.html', 'Equipe']);
  barra.innerHTML =
    `<a class="marca" href="/"><img src="/marca.svg" alt="">GeoLEM</a>` +
    `<nav>${itens.map(([id, href, t]) => `<a href="${href}"${id === atual ? ' aria-current="page"' : ''}>${t}</a>`).join('')}</nav>` +
    `<div class="conta"><span><b>${esc(eu.nome)}</b> · ${esc(eu.empresa.nome)}</span>` +
    `<button class="btn btn-peq" id="btn-sair" type="button">Sair</button></div>`;
  document.getElementById('btn-sair').addEventListener('click', async () => {
    await fetch('/api/auth/sair', { method: 'POST' });
    location.href = '/entrar.html';
  });
  document.dispatchEvent(new CustomEvent('geolem:usuario', { detail: eu }));
})();
