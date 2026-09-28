import json
import os

DATA_FILE = 'business_data.json'

def migrate_data():
    if not os.path.exists(DATA_FILE):
        print(f"File {DATA_FILE} not found!")
        return
        
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    if "config" not in data:
        data["config"] = {}
        
    # Start journal number
    next_id = 2026001
    
    # Process all existing entries
    if "euer" in data:
        for entry in data["euer"]:
            # Set Journal ID if not present
            if "journal_id" not in entry:
                entry["journal_id"] = f"J-{next_id}"
                next_id += 1
                
            # Append generic time to existing dates if missing
            if "datum" in entry and len(entry["datum"]) == 10: # "DD.MM.YYYY"
                entry["datum"] = entry["datum"] + " 12:00:00"
                
    # Update config
    data["config"]["next_journal_number"] = next_id
    
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        
    print(f"Migration successful! Updated {len(data.get('euer', []))} entries. Next Journal ID is {next_id}.")

if __name__ == '__main__':
    migrate_data()
