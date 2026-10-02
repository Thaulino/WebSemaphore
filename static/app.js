// The collapsed resource row carries the lock form and the label links.
// Without this, clicking either of them would fold the row instead.
document.querySelectorAll('summary input, summary button, summary a').forEach(
  function (el) {
    el.addEventListener('click', function (event) { event.stopPropagation(); });
    el.addEventListener('keydown', function (event) { event.stopPropagation(); });
  }
);

// Adds a show/hide switch to every password field marked data-reveal.
document.querySelectorAll('input[data-reveal]').forEach(function (input) {
  var wrapper = document.createElement('span');
  wrapper.className = 'reveal-wrap';
  input.parentNode.insertBefore(wrapper, input);
  wrapper.appendChild(input);

  var toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'reveal';
  toggle.textContent = 'Show';
  toggle.setAttribute('aria-label', 'Show password');
  wrapper.appendChild(toggle);

  toggle.addEventListener('click', function () {
    var hidden = input.type === 'password';
    input.type = hidden ? 'text' : 'password';
    toggle.textContent = hidden ? 'Hide' : 'Show';
    toggle.setAttribute('aria-label', (hidden ? 'Hide' : 'Show') + ' password');
    input.focus();
  });
});
