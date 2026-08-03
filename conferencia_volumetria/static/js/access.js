import { api } from "./api-client.js";
import { notify, notifyElement } from "./notifications.js";

const accessForm = document.querySelector("#access-form");
const accessInput = document.querySelector("#matricula");
const accessButton = document.querySelector("#btn-acessar");
const modal = document.querySelector("#cadastro-modal");
const registerForm = document.querySelector("#cadastro-form");
const registrationInput = document.querySelector("#cadastro-matricula");
const nameInput = document.querySelector("#cadastro-nome");
const shiftInput = document.querySelector("#cadastro-turno");
const registerButton = document.querySelector("#cadastrar");

let identifying = false;
let registering = false;
let editing = false;

document.querySelectorAll('input[type="text"], textarea').forEach((input) => {
  input.addEventListener("input", () => { input.value = input.value.toUpperCase(); });
});

function openRegistration() {
  editing = false;
  registerForm.reset();
  registrationInput.readOnly = false;
  registerButton.textContent = "Cadastrar";
  notifyElement("#cadastro-msg", "");
  modal.showModal();
  registrationInput.focus();
}

document.querySelector("#btn-abrir-cadastro-colaborador").addEventListener("click", openRegistration);

document.querySelector("#btn-editar-colaborador").addEventListener("click", async () => {
  const registration = accessInput.value.trim();
  if (!registration) {
    notify("Informe a matrícula do colaborador para editar.", "error");
    accessInput.focus();
    return;
  }
  try {
    const collaborator = await api(`/api/colaboradores/${encodeURIComponent(registration)}`);
    editing = true;
    registrationInput.value = collaborator.matricula;
    registrationInput.readOnly = true;
    nameInput.value = collaborator.nome;
    shiftInput.value = collaborator.turno;
    registerButton.textContent = "Salvar alterações";
    notifyElement("#cadastro-msg", "");
    modal.showModal();
    nameInput.focus();
  } catch (error) {
    notify(error.message, "error");
    accessInput.focus();
  }
});

function closeModal() {
  if (!registering) modal.close();
  accessInput.focus();
}

document.querySelector("#fechar-modal").addEventListener("click", closeModal);
document.querySelector("#cancelar").addEventListener("click", closeModal);

registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (registering) return;
  registering = true;
  registerButton.disabled = true;
  const registration = registrationInput.value.trim();
  try {
    const result = await api(
      editing
        ? `/api/colaboradores/${encodeURIComponent(registration)}`
        : "/api/colaboradores/cadastro-rapido",
      {
        method: "POST",
        body: JSON.stringify({ nome: nameInput.value.trim().toUpperCase(), matricula: registration, turno: shiftInput.value }),
      },
    );
    const collaborator = result.colaborador;
    accessInput.value = collaborator.matricula;
    modal.close();
    notify(editing ? "Colaborador atualizado." : "Colaborador cadastrado. Clique em Acessar para continuar.");
    accessButton.focus();
  } catch (error) {
    notifyElement("#cadastro-msg", error.message, "error");
    shiftInput.focus();
  } finally {
    registering = false;
    registerButton.disabled = false;
  }
});

accessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (identifying) return;
  identifying = true;
  accessButton.disabled = true;
  try {
    const data = await api("/api/access", {
      method: "POST",
      body: JSON.stringify({ matricula: accessInput.value.trim() }),
    });
    sessionStorage.setItem("conference_collaborator", JSON.stringify(data.colaborador));
    window.location.assign(data.redirect_url);
  } catch (error) {
    notify(error.message, "error");
    accessInput.select();
  } finally {
    identifying = false;
    accessButton.disabled = false;
  }
});
