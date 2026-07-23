/**
 * Pages Authentication - CORRIGIDO PARA SAFARI
 */

'use strict';

document.addEventListener('DOMContentLoaded', function (e) {
  (function () {
    const formAuthentication = document.querySelector('#formAuthentication');
    const btnSubmit = document.getElementById('btnSubmit');
    const btnText = document.getElementById('btnText');
    const btnLoader = document.getElementById('btnLoader');
    const numeralMask = document.querySelectorAll('.numeral-mask');

    if (formAuthentication) {
      
      let validationFields = {};

      const addValidation = (fieldName, rules) => {
        if (formAuthentication.querySelector(`[name="${fieldName}"]`)) {
          validationFields[fieldName] = rules;
        }
      };

      addValidation('username', {
        validators: {
          notEmpty: { message: 'Por favor insira um username' },
          stringLength: { min: 4, message: 'O username deve ter mais de 4 caracteres' }
        }
      });

      addValidation('email', {
        validators: {
          notEmpty: { message: 'Por favor insira o seu email' },
          emailAddress: { message: 'Por favor insira um email válido' }
        }
      });

      addValidation('empresa_nome', {
        validators: {
          notEmpty: { message: 'Por favor insira o nome da empresa' }
        }
      });

      addValidation('terms', {
        validators: {
          notEmpty: { message: 'Deve concordar com os termos e condições' }
        }
      });

      // Campo de Login (Só adiciona se existir)
      addValidation('email-username', {
        validators: {
          notEmpty: { message: 'Por favor insira email ou username' },
          stringLength: { min: 4, message: 'Mínimo de 4 caracteres' }
        }
      });

      // Password (Comum a ambos)
      addValidation('password', {
        validators: {
          notEmpty: { message: 'Por favor insira a password' },
          stringLength: { min: 4, message: 'A password deve ter mais de 4 caracteres' }
        }
      });

      // --- INICIALIZAÇÃO DO PLUGIN ---
      const fv = FormValidation.formValidation(formAuthentication, {
        fields: validationFields, // Usa a lista filtrada
        plugins: {
          trigger: new FormValidation.plugins.Trigger(),
          bootstrap5: new FormValidation.plugins.Bootstrap5({
            eleValidClass: '',
            rowSelector: '.mb-3, .col-md-6, .mb-0, .my-4, .mb-6' 
          }),
          submitButton: new FormValidation.plugins.SubmitButton(),
          autoFocus: new FormValidation.plugins.AutoFocus()
        },
        init: instance => {
          instance.on('plugins.message.placed', function (e) {
            if (e.element.parentElement.classList.contains('input-group')) {
              e.element.parentElement.insertAdjacentElement('afterend', e.messageElement);
            }
          });
        }
      });

      // 2. Evento do Botão (Lógica de Submit)
      if (btnSubmit) {
        btnSubmit.addEventListener('click', function (event) {
          event.preventDefault(); 
          
          fv.validate().then(function (status) {
            if (status === 'Valid') {
              // Animação
              if (btnText && btnLoader) {
                  btnSubmit.classList.add('disabled');
                  btnText.textContent = 'A processar...';
                  btnLoader.classList.remove('visually-hidden');
              }
              // Enviar formulário
              formAuthentication.submit();
            }
          });
        });
      }
    }

    // 3. Máscara para números (Apenas Registo)
    if (numeralMask.length) {
      numeralMask.forEach(e => {
        if (typeof Cleave !== 'undefined') {
            new Cleave(e, {
              numeral: true,
              numeralPositiveOnly: true,
              delimiter: ''
            });
        }
      });
    }
  })();
});