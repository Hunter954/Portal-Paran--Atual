(() => {
  const sidebar = document.getElementById('adminSidebar');
  const openBtn = document.querySelector('[data-admin-sidebar-open]');
  const closeBtns = document.querySelectorAll('[data-admin-sidebar-close]');
  const overlay = document.querySelector('.admin-overlay');

  const openSidebar = () => {
    if (!sidebar) return;
    sidebar.classList.add('is-open');
    overlay?.classList.add('is-open');
  };

  const closeSidebar = () => {
    sidebar?.classList.remove('is-open');
    overlay?.classList.remove('is-open');
  };

  openBtn?.addEventListener('click', openSidebar);
  closeBtns.forEach((btn) => btn.addEventListener('click', closeSidebar));
})();

(() => {
  const editor = document.querySelector('[data-ads-order-editor]');
  if (!editor) return;

  const sortables = [...editor.querySelectorAll('[data-ads-sortable]')];
  const preview = editor.querySelector('[data-ads-sortable="preview"]');
  const saveButton = editor.querySelector('[data-save-ads-order]');
  const status = editor.querySelector('[data-ads-order-status]');
  const saveUrl = editor.dataset.saveUrl;
  let draggedKey = null;
  let dirty = false;

  const currentOrder = () => [...preview.querySelectorAll('[data-slot-key]')].map(item => item.dataset.slotKey);

  const syncAll = (order) => {
    sortables.forEach(container => {
      order.forEach(key => {
        const item = container.querySelector(`[data-slot-key="${key}"]`);
        if (item) container.appendChild(item);
      });
    });
  };

  const markDirty = () => {
    dirty = true;
    saveButton.disabled = false;
    status.textContent = 'Ordem alterada. Clique em salvar para aplicar no site.';
    status.className = 'ads-order-status is-dirty';
  };

  const moveKey = (key, direction) => {
    const order = currentOrder();
    const index = order.indexOf(key);
    const next = direction === 'up' ? index - 1 : index + 1;
    if (index < 0 || next < 0 || next >= order.length) return;
    [order[index], order[next]] = [order[next], order[index]];
    syncAll(order);
    markDirty();
  };

  sortables.forEach(container => {
    container.addEventListener('dragstart', event => {
      const item = event.target.closest('[data-slot-key]');
      if (!item) return;
      draggedKey = item.dataset.slotKey;
      item.classList.add('is-dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', draggedKey);
    });

    container.addEventListener('dragover', event => {
      event.preventDefault();
      const target = event.target.closest('[data-slot-key]');
      container.querySelectorAll('.is-drag-over').forEach(el => el.classList.remove('is-drag-over'));
      if (target && target.dataset.slotKey !== draggedKey) target.classList.add('is-drag-over');
    });

    container.addEventListener('drop', event => {
      event.preventDefault();
      const target = event.target.closest('[data-slot-key]');
      if (!draggedKey || !target || target.dataset.slotKey === draggedKey) return;
      const order = currentOrder().filter(key => key !== draggedKey);
      const targetIndex = order.indexOf(target.dataset.slotKey);
      const rect = target.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      order.splice(targetIndex + (after ? 1 : 0), 0, draggedKey);
      syncAll(order);
      markDirty();
    });

    container.addEventListener('dragend', () => {
      draggedKey = null;
      editor.querySelectorAll('.is-dragging,.is-drag-over').forEach(el => el.classList.remove('is-dragging','is-drag-over'));
    });
  });

  editor.addEventListener('click', event => {
    const button = event.target.closest('[data-move]');
    if (!button) return;
    const item = button.closest('[data-slot-key]');
    moveKey(item.dataset.slotKey, button.dataset.move);
  });

  saveButton.addEventListener('click', async () => {
    if (!dirty) return;
    saveButton.disabled = true;
    status.textContent = 'Salvando nova ordem...';
    status.className = 'ads-order-status';
    try {
      const response = await fetch(saveUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify({order: currentOrder()}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || 'Não foi possível salvar.');
      dirty = false;
      status.textContent = data.message;
      status.className = 'ads-order-status is-success';
    } catch (error) {
      saveButton.disabled = false;
      status.textContent = error.message || 'Erro ao salvar a ordem.';
      status.className = 'ads-order-status is-error';
    }
  });
})();
