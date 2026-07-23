# Documentação de Software: Plataforma Orlis

## 1. Visão Geral
A Plataforma Orlis é uma solução *Full-Stack* para gestão técnica de chillers. O software combina monitorização em tempo real (IoT) com um sistema de gestão de manutenção (CMMS), permitindo passar de uma manutenção baseada em calendário para uma manutenção baseada na condição real e horas de uso do equipamento.

---

## 2. Technology Stack

### Backend & Core
* **Linguagem:** Python 3.10+
* **Framework Web:** Django 5
* **Motor de Física:** `CoolProp` (Cálculo de propriedades termodinâmicas do fluido refrigerante).
* **Protocolo Industrial:** `pymodbus` (Comunicação TCP com o PLC).

### Bases de Dados (Arquitetura Híbrida)
* **PostgreSQL (Relacional):** Armazena a estrutura do negócio (Clientes, Equipamentos), configurações técnicas (Tipo de Gás, Ciclo) e o histórico de manutenções (CMMS).
* **MongoDB (NoSQL):** Armazena a telemetria em alta frequência (Séries temporais de sensores a cada 5s).

### Frontend
* **Interface:** HTML5 / Bootstrap 5.
* **Visualização:** `ApexCharts.js` configurado com janelas deslizantes (Sliding Window) para gráficos em tempo real fluido.
* **Relatórios:** `WeasyPrint` para geração de PDFs.

---

## 3. Arquitetura e Módulos

### A. Serviço de Recolha (Data Collector)
Um *daemon* Python (`scriptV3.5.py`) corre continuamente em background:
1.  **Configuração Dinâmica:** Consulta o PostgreSQL para saber o IP, Gás (ex: R407C) e Ciclo (Verão/Inverno) de cada máquina.
2.  **Lógica de Estado:** Determina se o chiller está a trabalhar analisando o consumo elétrico (`MediaCorrentes > 2.0A`).
3.  **Processamento:**
    * Calcula o **C.O.P.** (Eficiência) e Entalpias em tempo real.
    * Aplica fator de desgaste (0.07) para realismo em máquinas antigas.
4.  **Persistência:**
    * Grava leituras no **MongoDB**.
    * Incrementa o **Horímetro** na tabela do **PostgreSQL** (apenas se ativo).

### B. Dashboard Web (Monitorização)
Interface para visualização de dados:
* **Gráficos em Tempo Real:** Atualização via AJAX (5s) com animação suave.
* **Eixos Dinâmicos:** Formatação automática para 2 casas decimais.
* **Alertas:** Indicação visual de estado (Ligado/Standby/Desativado) baseada na prioridade (Status Admin SQL > Status Sensor Mongo).

### C. CMMS (Gestão de Manutenção)
Módulo de Livro de Obra Digital integrado:
* **Registo de Intervenções:** Tipificação (Preventiva, Corretiva, F-Gas).
* **Conformidade F-Gas:** Campos obrigatórios para carga de gás (kg) e testes de fugas.
* **Ciclo de Vida:** Gestão do contador de horas com funcionalidade de "Reset" protegida (apenas para revisões gerais).

---

## 4. Estrutura de Dados Crítica

### Tabela `Chiller` (Postgres)
Define a "identidade" da máquina e parâmetros físicos.
* `ipcontrolador`: Endereço Modbus.
* `gas`: Fluido refrigerante (fundamental para o cálculo do CoolProp).
* `horas_funcionamento`: Contador acumulado de desgaste.

### Tabela `Intervencao` (Postgres)
Registo histórico.
* `tipo`: Preventiva, Corretiva, Regulamentar.
* `gas_adicionado`: Rastreabilidade ambiental.
* `reset_horimetro`: Flag de reinício de ciclo.

---

## 5. Instalação e Execução

**Pré-requisitos:**
Instalar bibliotecas Python:
```bash
pip install asgiref==3.8.1 brotli==1.2.0 cffi==2.0.0 coolprop==6.8.0 cssselect2==0.8.0 Django==5.0.6 djangorestframework==3.16.1 dnspython==2.7.0 dotenv==0.9.9 fonttools==4.60.1 gunicorn==22.0.0 joblib==1.5.2 Markdown==3.10 numpy==2.3.5 packaging==24.1 pillow==12.0.0 psycopg==3.2.12 psycopg-binary==3.2.12 psycopg2-binary==2.9.10 pycparser==2.23 pydyf==0.11.0 pymodbus==3.9.2 pymongo==4.13.2 pyphen==0.17.2 python-dotenv==1.0.1 setuptools==80.9.0 sqlparse==0.5.0 tinycss2==1.5.0 tinyhtml5==2.0.0 typing_extensions==4.12.2 weasyprint==66.0 webencodings==0.5.1 wheel==0.45.1 whitenoise==6.7.0 zopfli==0.4.0