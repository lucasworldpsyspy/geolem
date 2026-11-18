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
  // Aviso de teste ou licença perto de vencer
  function aviso(e) {
    if (!e.valido_ate) return '';
    const dias = Math.ceil((new Date(e.valido_ate + 'T23:59:59') - new Date()) / 86400000);
    if (dias > 15) return '';
    const txt = e.situacao === 'teste' ? `Teste: ${dias} dia(s) restante(s)` : `Licença vence em ${dias} dia(s)`;
    return `<span class="selo warn" title="Fale com a GeoLEM para renovar">${txt}</span>`;
  }
  const itens = [['painel', '/painel.html', 'Painel'], ['carteira', '/carteira.html', 'Carteira'], ['analise', '/', 'Análise'],
    ['laudos', '/laudos.html', 'Laudos']];
  if (eu.papel === 'admin') itens.push(['equipe', '/equipe.html', 'Equipe']);
  if (eu.superadmin) itens.push(['admin', '/admin.html', 'Admin GeoLEM']);
  barra.innerHTML =
    `<a class="marca" href="/painel.html"><img src="/marca.svg" alt="">GeoLEM</a>` +
    `<nav>${itens.map(([id, href, t]) => `<a href="${href}"${id === atual ? ' aria-current="page"' : ''}>${t}</a>`).join('')}</nav>` +
    `<div class="conta">${aviso(eu.empresa)}<span><b>${esc(eu.nome)}</b> · ${esc(eu.empresa.nome)}</span>` +
    `<button class="btn btn-peq" id="btn-sair" type="button">Sair</button></div>`;
  document.getElementById('btn-sair').addEventListener('click', async () => {
    await fetch('/api/auth/sair', { method: 'POST' });
    location.href = '/entrar.html';
  });
  document.dispatchEvent(new CustomEvent('geolem:usuario', { detail: eu }));
})();
