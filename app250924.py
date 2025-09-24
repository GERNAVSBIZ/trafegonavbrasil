# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, jsonify, Response, render_template_string
import pandas as pd
import re
from datetime import datetime
import io
import os
import json
import firebase_admin
from firebase_admin import credentials, auth, firestore
# from weasyprint import HTML # Funcionalidade de PDF permanece desativada

app = Flask(__name__)
db = None
try:
    # Esta é a parte que vai funcionar no OnRender, lendo a variável de ambiente
    creds_json_str = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if creds_json_str:
        creds_dict = json.loads(creds_json_str)
        cred = credentials.Certificate(creds_dict)
    else:
        # Isto é um fallback para rodar localmente, se o arquivo existir
        cred_path = 'firebase-credentials.json'
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            print("AVISO: Credenciais do Firebase não encontradas. Funcionalidades do banco de dados estarão limitadas.")
            cred = None
    
    if cred:
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase Admin SDK e Firestore inicializados com sucesso.")
except Exception as e:
    print(f"ERRO: Falha ao inicializar o Firebase Admin SDK: {e}")

def parse_data_file(file_content, icao_code):
    lines = file_content.split('\n')
    records = []
    data_date_from_file = None

    for line in lines:
        line = line.strip()
        
        if not line or len(line) < 25:
            continue
        if re.search(r'\s+(MG|V)\s+\d{4}\s*$', line):
            continue

        record = {
            'timestamp': None, 'matricula': 'N/A', 'tipo_aeronave': 'N/A',
            'origem': 'N/A', 'destino': 'N/A', 'regra_voo': 'N/A',
            'pista': '', 'responsavel': 'N/A', 'flight_class': 'N/A',
            'aerodromo': icao_code
        }

        try:
            date_str_header = line[9:15]
            data_block = line[15:].strip()
            route_block = ''
            match = None
            patterns = [
                r'^(?P<matricula>(?:AZU|GLO|TAM)\d{4})(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)',
                r'^(?P<matricula>FAB\d+)(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)',
                r'^(?P<matricula>N[A-Z0-9]+)(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)',
                r'^(?P<matricula>\S+)\s+(?P<tipo>[A-Z0-9]+)\s+(?P<classe>[GSNM])\s+(?P<resto>.*)',
                r'^(?P<matricula>\S+)\s+(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)',
            ]
            for pattern in patterns:
                match = re.match(pattern, data_block)
                if match: break
            if not match: continue
            g = match.groupdict()
            record['matricula'] = g['matricula']
            route_block = g['resto'].strip()
            if 'tipo_classe' in g:
                type_class_str = g['tipo_classe']
                record['tipo_aeronave'] = type_class_str[:-1]
                record['flight_class'] = type_class_str[-1]
            else:
                record['tipo_aeronave'] = g['tipo']
                record['flight_class'] = g['classe']
            op_match = re.search(r'\s([A-Z]{4})$', route_block)
            if op_match:
                record['responsavel'] = op_match.group(1)
                route_block = route_block[:op_match.start()].strip()
            pista_match = re.search(r'\s(07|25)$', route_block)
            if pista_match:
                record['pista'] = pista_match.group(1)
                route_block = route_block[:pista_match.start()].strip()
            rule_match = re.search(r'(IV|VV)', route_block)
            if rule_match:
                record['regra_voo'] = 'IFR' if rule_match.group(1) == 'IV' else 'VFR'
            horario_str = ''
            waypoints = []
            overflight_match = re.search(r'([A-Z0-9]{4}).*?(\d{4}).*?([A-Z0-9]{4})', route_block)
            if overflight_match:
                waypoints.append(overflight_match.group(1))
                horario_str = overflight_match.group(2)
                waypoints.append(overflight_match.group(3))
            else:
                single_move_match = re.search(r'([A-Z0-9]{4}).*?(\d{4})', route_block)
                if single_move_match:
                    waypoints.append(single_move_match.group(1))
                    horario_str = single_move_match.group(2)
                else:
                    time_only_match = re.search(r'(\d{4})', route_block)
                    if time_only_match:
                        horario_str = time_only_match.group(1)
            if len(waypoints) >= 2:
                record['destino'] = waypoints[0]
                record['origem'] = waypoints[1]
            elif len(waypoints) == 1:
                record['origem'] = icao_code
                record['destino'] = waypoints[0]
            else:
                record['origem'] = icao_code
                record['destino'] = icao_code
            if horario_str:
                dt_obj = datetime.strptime(f"{date_str_header}{horario_str}", '%d%m%y%H%M')
                record['timestamp'] = dt_obj.isoformat() + 'Z'
                if data_date_from_file is None:
                    data_date_from_file = record['timestamp']
            
            records.append(record)

        except Exception as e:
            print(f"ERRO ao processar linha: '{line.strip()}'. Erro: {e}")

    return {"records": records, "icao_code": icao_code, "data_date": data_date_from_file}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    # Bloco de segurança REATIVADO
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        print(f"Upload autorizado para o usuário: {decoded_token['uid']}")
    except Exception as e:
        return jsonify({"error": "Token inválido ou expirado"}), 401
    
    files = request.files.getlist('dataFiles')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    grouped_records = []

    for file in files:
        if file.filename != '':
            try:
                file_path_parts = file.filename.split('/')
                actual_filename = file_path_parts[-1]
                
                icao_code_match = re.match(r'^([A-Z]{4})', actual_filename)
                if not icao_code_match:
                    print(f"AVISO: Não foi possível determinar o ICAO para o arquivo '{actual_filename}'. Pulando.")
                    continue
                
                icao_code = icao_code_match.group(1).upper()
                
                content = io.StringIO(file.stream.read().decode("utf-8", errors='ignore')).getvalue()
                
                parsed_data = parse_data_file(content, icao_code)
                
                if parsed_data["records"]:
                    grouped_records.append({
                        "fileName": actual_filename,
                        "records": parsed_data["records"],
                        "icao_code": parsed_data["icao_code"],
                        "data_date": parsed_data["data_date"]
                    })
            except Exception as e:
                print(f"Erro ao processar o arquivo {file.filename}: {e}")
                continue

    if not grouped_records:
        return jsonify({"error": "Nenhum registro válido encontrado nos arquivos"}), 400

    return jsonify({ "grouped_records": grouped_records })

"""
# ROTA DE PDF DESATIVADA
@app.route('/api/generate_report', methods=['POST'])
def generate_pdf_report():
    # ... código do PDF ...
"""

@app.route('/api/save_records', methods=['POST'])
def save_records():
    # Bloco de segurança REATIVADO
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception as e:
        return jsonify({"error": "Token inválido ou expirado"}), 401

    if not db:
        return jsonify({"error": "Conexão com o banco de dados não está disponível"}), 500
    try:
        uploads_to_save = request.get_json()
        if not uploads_to_save or not isinstance(uploads_to_save, list):
            return jsonify({"error": "Dados inválidos ou vazios"}), 400

        saved_count = 0
        for upload_data in uploads_to_save:
            records_to_save = upload_data.get('records')
            icao_code = upload_data.get('icao_code')
            data_date = upload_data.get('data_date')
            
            if not records_to_save: continue

            upload_ref = db.collection('flight_uploads').document()
            upload_ref.set({
                'userId': user_id, 'createdAt': firestore.SERVER_TIMESTAMP,
                'recordCount': len(records_to_save), 'icaoCode': icao_code, 'dataDate': data_date
            })
            
            batch = db.batch()
            for i, rec in enumerate(records_to_save):
                doc_ref = upload_ref.collection('records').document()
                batch.set(doc_ref, rec)
                if (i + 1) % 500 == 0:
                    batch.commit()
                    batch = db.batch()
            batch.commit()
            saved_count += 1
        
        return jsonify({"success": True, "message": f"{saved_count} arquivo(s) salvo(s) com sucesso!"}), 201
    except Exception as e:
        print(f"ERRO ao salvar no Firestore: {e}")
        return jsonify({"error": f"Erro interno ao salvar os dados: {str(e)}"}), 500

@app.route('/api/get_uploads', methods=['GET'])
def get_uploads():
    # Bloco de segurança REATIVADO
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception as e:
        return jsonify({"error": "Autenticação falhou"}), 401
    
    if not db:
        return jsonify([]), 200

    try:
        uploads_ref = db.collection('flight_uploads')
        query = uploads_ref.where('userId', '==', user_id).order_by('createdAt', direction=firestore.Query.DESCENDING)
        results = []
        for doc in query.stream():
            doc_data = doc.to_dict()
            results.append({
                'uploadId': doc.id, 'createdAt': doc_data['createdAt'].isoformat(),
                'recordCount': doc_data.get('recordCount'), 'icaoCode': doc_data.get('icaoCode', None),
                'dataDate': doc_data.get('dataDate', None)
            })
        return jsonify(results), 200
    except Exception as e:
        print(f"ERRO ao buscar uploads: {e}")
        return jsonify({"error": "Não foi possível buscar o histórico de uploads."}), 500

@app.route('/api/get_records/<upload_id>', methods=['GET'])
def get_records(upload_id):
    # Bloco de segurança REATIVADO
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception as e:
        return jsonify({"error": "Autenticação falhou"}), 401
    try:
        upload_doc = db.collection('flight_uploads').document(upload_id).get()
        if not upload_doc.exists or upload_doc.to_dict()['userId'] != user_id:
            return jsonify({"error": "Acesso não autorizado ou upload não encontrado"}), 403
        records_ref = db.collection('flight_uploads').document(upload_id).collection('records')
        records = [doc.to_dict() for doc in records_ref.stream()]
        return jsonify(records), 200
    except Exception as e:
        print(f"ERRO ao buscar registros do upload {upload_id}: {e}")
        return jsonify({"error": "Não foi possível buscar os registros."}), 500

def delete_collection(coll_ref, batch_size):
    while True:
        docs = coll_ref.limit(batch_size).stream()
        batch = db.batch()
        doc_count = 0
        for doc in docs:
            batch.delete(doc.reference)
            doc_count += 1
        if doc_count > 0: batch.commit()
        if doc_count < batch_size: break

@app.route('/api/delete_upload/<upload_id>', methods=['DELETE'])
def delete_upload(upload_id):
    # Bloco de segurança REATIVADO
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception as e:
        return jsonify({"error": "Autenticação falhou"}), 401
    try:
        upload_ref = db.collection('flight_uploads').document(upload_id)
        upload_doc = upload_ref.get()
        if not upload_doc.exists: return jsonify({"error": "Upload não encontrado"}), 404
        if upload_doc.to_dict()['userId'] != user_id: return jsonify({"error": "Acesso não autorizado"}), 403
        
        records_ref = upload_ref.collection('records')
        delete_collection(records_ref, 500)
        upload_ref.delete()
        
        return jsonify({"success": True, "message": "Registro apagado com sucesso!"}), 200
    except Exception as e:
        print(f"ERRO ao apagar o upload {upload_id}: {e}")
        return jsonify({"error": "Não foi possível apagar o registro."}), 500

@app.route('/api/get_aggregated_data', methods=['GET'])
def get_aggregated_data():
    # Bloco de segurança REATIVADO
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception as e:
        return jsonify({"error": "Autenticação falhou"}), 401
    
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    if not start_date_str or not end_date_str:
        return jsonify({"error": "As datas de início e fim são obrigatórias"}), 400
    try:
        start_date = datetime.fromisoformat(start_date_str + 'T00:00:00')
        end_date = datetime.fromisoformat(end_date_str + 'T23:59:59')

        uploads_ref = db.collection('flight_uploads')
        start_iso = start_date.isoformat() + 'Z'
        end_iso = end_date.isoformat() + 'Z'

        query = uploads_ref.where('userId', '==', user_id).where('dataDate', '>=', start_iso).where('dataDate', '<=', end_iso)
        
        relevant_uploads = [doc.id for doc in query.stream()]
        all_records = []
        for upload_id in relevant_uploads:
            records_ref = db.collection('flight_uploads').document(upload_id).collection('records')
            records = [doc.to_dict() for doc in records_ref.stream()]
            all_records.extend(records)
        return jsonify(all_records), 200
    except Exception as e:
        print(f"ERRO ao agregar dados: {e}")
        return jsonify({"error": "Não foi possível processar a solicitação."}), 500

# Este bloco só é executado quando você roda 'python app.py' localmente.
# O Gunicorn não executa este bloco.
if __name__ == '__main__':
    app.run(debug=True)
