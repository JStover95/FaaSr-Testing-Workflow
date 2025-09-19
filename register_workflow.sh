WORKFLOW_FILE="main.json"

while [[ $# -gt 0 ]]; do
    case $1 in
        --workflow-file)
            WORKFLOW_FILE=$2
            shift 2
            ;;
        *)
            echo "Invalid argument: $1"
            exit 1
    esac
done

export $(cat .env | xargs)
python scripts/register_workflow.py --workflow-file $WORKFLOW_FILE
git pull
