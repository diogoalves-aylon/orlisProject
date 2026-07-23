# 🌡️ Orlis - Plataforma de Monitorização de Chillers 

![Status](https://img.shields.io/badge/Status-V3.5_Pro-blue)
![Stack](https://img.shields.io/badge/Stack-Django_|_Python_|_Modbus-green)
![Engineering](https://img.shields.io/badge/Focus-CMMS_&_Thermodynamics-orange)

## 🧭 Objetivo
A **Orlis** é uma solução *Full-Stack* de **Monotorização e gestão de Chillers (AVACS)**.

O sistema integra a automação industrial (PLC Eliwell) com a gestão web, permitindo:
1.  **Monitorização em Tempo Real:** Telemetria de alta frequência (5s).
2.  **Manutenção Preditiva:** Transição de manutenção baseada em calendário para manutenção baseada em **horas reais de funcionamento** e condição da máquina.
3.  **Engenharia:** Cálculo automático de eficiência (C.O.P.) e conformidade legal (F-Gas).

---

## 📚 Documentação Técnica

Para detalhes aprofundados sobre a implementação, consulta a pasta `/docs`:

* 🛠 **[Hardware & Automação](docs/hardware.md)**: PLC Eliwell AVD6200, Medidor IEM3255 e Mapeamento Modbus.
* 💻 **[Software & Arquitetura](docs/software.md)**: Lógica do Script Python, CoolProp, Django e Bases de Dados.

---

## 🧩 Arquitetura do Sistema

O projeto utiliza uma **Arquitetura Híbrida** para otimizar a escrita de dados de sensores e a integridade da gestão.

### 1. Controlador IoT (Edge)
* **Hardware:** Eliwell AVD6200 + IEM3255.
* **Função:** Concentrador de dados (Sensores + Energia) via Modbus RTU/TCP.

### 2. Middleware & Recolha (Python Service)
* **Daemon:** `scriptV3.5.py` (Corre em background).
* **Lógica:**
    * Lê Modbus TCP a cada 5 segundos.
    * **Física:** Calcula Entalpias e COP usando `CoolProp` (R407C/R410A dinâmico).
    * **Gestão:** Deteta estado de carga (Corrente > 2A) e incrementa o **Horímetro Real** no PostgreSQL.

### 3. Backend & Bases de Dados
* **Django 5:** Gestão de utilizadores, lógica de negócio e API.
* **PostgreSQL:** Dados estruturados (Clientes, Equipamentos, Livro de Obra, Horas de Funcionamento).
* **MongoDB:** Séries temporais (Telemetry) para gráficos de alta resolução.

### 4. Frontend (Visualização)
* **ApexCharts:** Gráficos com janelas deslizantes (Sliding Window) para efeito de tempo real fluido.
* **WeasyPrint:** Geração de relatórios técnicos em PDF.

---

## ✨ Novas Funcionalidades (V3.5)

### 🔧 CMMS & Livro de Obra Digital
* **Registo Técnico:** Diferenciação entre Preventiva, Corretiva e Preditiva.
* **Gestão F-Gas:** Campos obrigatórios para registo de carga de gás (kg) e testes de fugas (compliance ambiental).
* **Proteção de Ciclo de Vida:** O "Reset" das horas do compressor exige confirmação explicita de revisão geral.

### ⚡ Eficiência Energética
* **Cálculo de COP Real:** Monitorização da eficiência termodinâmica instantânea.
* **Custo Operacional:** Cálculo de €/h em tempo real baseado no tarifário energético.

---

## 🧰 Tecnologias Utilizadas

| Categoria | Tecnologias |
|------------|--------------|
| **Backend** | Python 3.14, Django 5.0 |
| **Recolha IoT** | `pymodbus`, `psycopg2` |
| **Engenharia** | `CoolProp` (Termodinâmica), `numpy` |
| **Bases de Dados** | PostgreSQL (Relacional) + MongoDB (NoSQL) |
| **Frontend** | Bootstrap 5, **ApexCharts.js** (Substituiu Chart.js) |
| **Relatórios** | WeasyPrint (PDF Engine) |
| **Infraestrutura** | Docker (Opcional), Systemd Services |

---

## 🚀 Instalação e Execução

### 1. Instalar Dependências
```bash
pip install -r requirements.txt
